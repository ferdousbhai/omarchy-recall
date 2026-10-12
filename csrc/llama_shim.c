// EmbeddingGemma 2 on llama.cpp, for the Rust engine (src/llama.rs): a small C API over llama.cpp's and mtmd's, so
// Rust never touches their structs, whose layout changes between llama.cpp versions.
//
// The image and audio encoders (mtmd) feed the text model, and its mean-pooled output is the vector (Rust normalizes
// it). Inputs are framed as the reference implementation (transformers) frames them: <bos> text <eos>,
// <bos> <|image> soft tokens <image|> <eos>, and the same for audio. The model is non-causal with no KV cache, so each
// batch of inputs, token ids and encoder rows together, goes through one llama_process call, as llama-server's mixed
// batches do. Measured against the reference in bf16: cosine >= 0.999 on text and images, 0.9998 on a 30 s window.
//
// One thread at a time per embedder.

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ggml-backend.h"
#include "llama.h"
#include "mtmd-helper.h"
#include "mtmd.h"

#define MAX_SEQUENCES 16

// MARK: Logs

static FILE * log_file = NULL;
static int log_continues = 0;   // the last line kept was a warning or an error, and this is its continuation

static void log_callback(enum ggml_log_level level, const char * text, void * user_data) {
    (void) user_data;
    int keep = level == GGML_LOG_LEVEL_WARN || level == GGML_LOG_LEVEL_ERROR
        || (level == GGML_LOG_LEVEL_CONT && log_continues);
    if (level != GGML_LOG_LEVEL_CONT) log_continues = keep;
    if (!keep) return;
    FILE * out = log_file ? log_file : stderr;
    fputs(text, out);
    fflush(out);
}

// Where llama.cpp's warnings and errors go: a file (appended), or stderr when path is NULL.
void recall_log_to(const char * path) {
    if (log_file) { fclose(log_file); log_file = NULL; }
    if (path) log_file = fopen(path, "a");
}

// A line of our own in the same log.
void recall_log_line(const char * text) {
    FILE * out = log_file ? log_file : stderr;
    fprintf(out, "[llama.cpp worker] %s\n", text);
    fflush(out);
}

// MARK: Backends and the device

static int backend_ready = 0;
static ggml_backend_dev_t chosen_device = NULL;
static ggml_backend_dev_t device_list[2] = { NULL, NULL };

static int starts_with_lower(const char * text, const char * prefix) {
    for (; *prefix; text++, prefix++) {
        char c = *text;
        if (c >= 'A' && c <= 'Z') c = (char) (c - 'A' + 'a');
        if (c != *prefix) return 0;
    }
    return 1;
}

// Loads the backend plugins in `folder` (CUDA, Vulkan, the CPU variants this machine can run) and picks the device:
// the first GPU in llama.cpp's registration order, which puts CUDA before Vulkan, then an integrated GPU, else the CPU.
// `wanted` ("cpu", "cuda", "vulkan" or "") picks one by hand. Once per process.
void recall_backend_init(const char * folder, const char * wanted) {
    if (backend_ready) return;
    backend_ready = 1;
    llama_log_set(log_callback, NULL);
    mtmd_helper_log_set(log_callback, NULL);
    ggml_backend_load_all_from_path(folder);
    llama_backend_init();
    size_t count = ggml_backend_dev_count();
    if (wanted && *wanted) {
        if (strcmp(wanted, "cpu") != 0) {
            for (size_t i = 0; i < count; i++) {
                ggml_backend_dev_t device = ggml_backend_dev_get(i);
                if (starts_with_lower(ggml_backend_reg_name(ggml_backend_dev_backend_reg(device)), wanted)) {
                    chosen_device = device;
                    break;
                }
            }
        }
    } else {
        for (size_t i = 0; i < count && !chosen_device; i++) {
            ggml_backend_dev_t device = ggml_backend_dev_get(i);
            if (ggml_backend_dev_type(device) == GGML_BACKEND_DEVICE_TYPE_GPU) chosen_device = device;
        }
        for (size_t i = 0; i < count && !chosen_device; i++) {
            ggml_backend_dev_t device = ggml_backend_dev_get(i);
            if (ggml_backend_dev_type(device) == GGML_BACKEND_DEVICE_TYPE_IGPU) chosen_device = device;
        }
    }
    device_list[0] = chosen_device;
}

// What the device is, for logs: "Vulkan0 (Intel(R) Iris(R) Xe Graphics)", "CPU".
void recall_device_name(char * out, size_t size) {
    if (!chosen_device) { snprintf(out, size, "CPU"); return; }
    snprintf(out, size, "%s (%s)", ggml_backend_dev_name(chosen_device), ggml_backend_dev_description(chosen_device));
}

// MARK: Image sizing

// transformers' Gemma 4 image processor: scale to fill `budget` soft tokens (each 3 x 3 patches of 16 px) keeping the
// aspect ratio, each side rounded down to a multiple of 48 px. Returns 0 when the image is too thin.
int recall_target_size(int height, int width, int budget, int * out_height, int * out_width) {
    const int patch = 16, pooling = 3;
    int max_patches = budget * pooling * pooling;
    double factor = sqrt((double) (max_patches * patch * patch) / (double) (height * width));
    int side = pooling * patch;
    int target_height = (int) floor(factor * height / side) * side;
    int target_width = (int) floor(factor * width / side) * side;
    if (target_height == 0 && target_width == 0) return 0;
    int max_side = (max_patches / (pooling * pooling)) * side;
    if (target_height == 0) {
        target_height = side;
        target_width = width / height * side < max_side ? width / height * side : max_side;
    } else if (target_width == 0) {
        target_width = side;
        target_height = height / width * side < max_side ? height / width * side : max_side;
    }
    *out_height = target_height;
    *out_width = target_width;
    return 1;
}

#define PRECISION 22

static double bicubic(double value) {
    double x = fabs(value), a = -0.5;
    if (x < 1) return ((a + 2) * x - (a + 3)) * x * x + 1;
    if (x < 2) return (((x - 5) * x + 8) * x - 4) * a;
    return 0;
}

// For each output pixel: the first contributing input pixel, how many contribute, and their weights.
static int coefficients(int in_size, int out_size, int ** bounds_out, int32_t ** weights_out) {
    double scale = (double) in_size / (double) out_size;
    double filter_scale = scale > 1 ? scale : 1;
    double support = 2 * filter_scale;
    int size = (int) ceil(support) * 2 + 1;
    int * bounds = calloc((size_t) out_size * 2, sizeof(int));
    int32_t * weights = calloc((size_t) out_size * size, sizeof(int32_t));
    double * row = calloc((size_t) size, sizeof(double));
    for (int out = 0; out < out_size; out++) {
        double center = (out + 0.5) * scale;
        int first = (int) (center - support + 0.5);
        if (first < 0) first = 0;
        int end = (int) (center + support + 0.5);
        if (end > in_size) end = in_size;
        int count = end - first;
        double total = 0;
        for (int x = 0; x < size; x++) {
            row[x] = x < count ? bicubic((x + first - center + 0.5) / filter_scale) : 0;
            total += row[x];
        }
        for (int x = 0; x < size; x++) {
            double weight = x < count && total != 0 ? row[x] / total : row[x];
            weights[out * size + x] = (int32_t) (weight * (double) (1 << PRECISION) + (weight < 0 ? -0.5 : 0.5));
        }
        bounds[out * 2] = first;
        bounds[out * 2 + 1] = count;
    }
    free(row);
    *bounds_out = bounds;
    *weights_out = weights;
    return size;
}

static unsigned char clip(int32_t value) {
    value >>= PRECISION;
    return (unsigned char) (value < 0 ? 0 : value > 255 ? 255 : value);
}

static void horizontal(const unsigned char * src, int in_width, int rows, int out_width, unsigned char * out) {
    int * bounds; int32_t * weights;
    int size = coefficients(in_width, out_width, &bounds, &weights);
    for (int y = 0; y < rows; y++) {
        for (int x = 0; x < out_width; x++) {
            const unsigned char * p = src + ((size_t) y * in_width + bounds[x * 2]) * 3;
            uint32_t r = 1u << (PRECISION - 1), g = r, b = r;
            for (int k = 0; k < bounds[x * 2 + 1]; k++) {
                uint32_t w = (uint32_t) weights[x * size + k];
                r += (uint32_t) p[0] * w; g += (uint32_t) p[1] * w; b += (uint32_t) p[2] * w;
                p += 3;
            }
            size_t o = ((size_t) y * out_width + x) * 3;
            out[o] = clip((int32_t) r); out[o + 1] = clip((int32_t) g); out[o + 2] = clip((int32_t) b);
        }
    }
    free(bounds); free(weights);
}

static void vertical(const unsigned char * src, int in_height, int row_width, int out_height, unsigned char * out) {
    int * bounds; int32_t * weights;
    int size = coefficients(in_height, out_height, &bounds, &weights);
    size_t stride = (size_t) row_width * 3;
    uint32_t * sums = malloc(stride * sizeof(uint32_t));
    for (int y = 0; y < out_height; y++) {
        for (size_t i = 0; i < stride; i++) sums[i] = 1u << (PRECISION - 1);
        for (int k = 0; k < bounds[y * 2 + 1]; k++) {
            const unsigned char * row = src + (size_t) (bounds[y * 2] + k) * stride;
            uint32_t w = (uint32_t) weights[y * size + k];
            for (size_t i = 0; i < stride; i++) sums[i] += (uint32_t) row[i] * w;
        }
        for (size_t i = 0; i < stride; i++) out[(size_t) y * stride + i] = clip((int32_t) sums[i]);
    }
    free(sums); free(bounds); free(weights);
}

// Pillow's bicubic resampling of packed RGB (a = -0.5, the kernel widened when shrinking, 22-bit fixed point): what
// the reference gets from PIL's Image.resize. `out` holds to_width * to_height * 3 bytes.
void recall_resize_bicubic(const unsigned char * src, int width, int height, int to_width, int to_height,
                           unsigned char * out) {
    if (to_width != width && to_height != height) {
        unsigned char * pass = malloc((size_t) to_width * height * 3);
        horizontal(src, width, height, to_width, pass);
        vertical(pass, height, to_width, to_height, out);
        free(pass);
    } else if (to_width != width) {
        horizontal(src, width, height, to_width, out);
    } else if (to_height != height) {
        vertical(src, height, width, to_height, out);
    } else {
        memcpy(out, src, (size_t) width * height * 3);
    }
}

// MARK: The embedder

typedef struct recall_embedder {
    struct llama_model * model;
    struct llama_context * context;
    mtmd_context * media;
    struct llama_batch_ext * batch;
    const struct llama_vocab * vocab;
    int embedding_size;
    int row_size;            // the width of encoder rows fed to the text model
    int max_tokens;
    int sequences;           // in `batch`
    int tokens;              // in `batch`
    llama_pos position;
    int failed_here;         // an input couldn't be placed: the batch is reset before the next one
} recall_embedder;

static void set_error(char * error, size_t size, const char * format, ...) {
    if (!error || !size) return;
    va_list args;
    va_start(args, format);
    vsnprintf(error, size, format, args);
    va_end(args);
}

void recall_close(recall_embedder * e) {
    if (!e) return;
    if (e->batch) llama_batch_ext_free(e->batch);
    if (e->media) mtmd_free(e->media);
    if (e->context) llama_free(e->context);
    if (e->model) llama_model_free(e->model);
    free(e);
}

// Loads the text model and, unless `media_path` is NULL, the image and audio encoders. `max_tokens`: the longest input
// (2048 holds a 1120-token image or a 30 s clip; queries need 512).
recall_embedder * recall_open(const char * text_path, const char * media_path, int max_tokens,
                              char * error, size_t error_size) {
    recall_embedder * e = calloc(1, sizeof(recall_embedder));
    struct llama_model_params model_params = llama_model_default_params();
    model_params.n_gpu_layers = chosen_device ? 999 : 0;
    model_params.devices = device_list;   // just the chosen one: CUDA and Vulkan can both see the same card
    e->model = llama_model_load_from_file(text_path, model_params);
    if (!e->model) { set_error(error, error_size, "can't load %s", text_path); recall_close(e); return NULL; }
    struct llama_context_params params = llama_context_default_params();
    params.n_ctx = (uint32_t) max_tokens;
    params.n_batch = (uint32_t) max_tokens;
    params.n_ubatch = (uint32_t) max_tokens;
    params.n_seq_max = MAX_SEQUENCES;
    params.kv_unified = true;   // each input may use the whole context, as in llama-embedding
    params.embeddings = true;
    params.pooling_type = LLAMA_POOLING_TYPE_MEAN;
    e->context = llama_init_from_model(e->model, params);
    if (!e->context) { set_error(error, error_size, "can't create a llama.cpp context"); recall_close(e); return NULL; }
    if (media_path) {
        struct mtmd_context_params media_params = mtmd_context_params_default();
        media_params.use_gpu = chosen_device != NULL;
        media_params.device = chosen_device;
        media_params.print_timings = false;
        media_params.warmup = true;
        media_params.image_min_tokens = 1;      // never rescale: images arrive at their final size
        media_params.image_max_tokens = 1120;
        e->media = mtmd_init_from_file(media_path, e->model, media_params);
        if (!e->media) { set_error(error, error_size, "can't load %s", media_path); recall_close(e); return NULL; }
    }
    e->batch = llama_batch_ext_init(e->context);
    e->vocab = llama_model_get_vocab(e->model);
    e->embedding_size = llama_model_n_embd_out(e->model);
    e->row_size = llama_model_n_embd_inp(e->model);
    e->max_tokens = max_tokens;
    return e;
}

int recall_embedding_size(const recall_embedder * e) { return e->embedding_size; }

static void reset(recall_embedder * e) {
    llama_batch_ext_clear(e->batch);
    e->sequences = 0;
    e->tokens = 0;
}

static void begin(recall_embedder * e) {
    e->sequences += 1;
    e->position = 0;
}

static int place(recall_embedder * e, int32_t index) {
    if (index < 0) return 0;
    llama_pos pos[4] = { e->position, 0, 0, 0 };
    if (!llama_batch_ext_set_pos(e->batch, index, pos) || !llama_batch_ext_set_output_embd(e->batch, index, true)) {
        return 0;
    }
    e->position += 1;
    e->tokens += 1;
    return 1;
}

// Runs the text model over the batch and writes one pooled vector per input, in order, to `out`. Returns 0 on success.
static int run(recall_embedder * e, float * out, char * error, size_t error_size) {
    int32_t status = llama_process(e->context, LLAMA_PROCESS_TYPE_ENCODE, e->batch);
    if (status != 0) { set_error(error, error_size, "llama_process failed (%d)", status); reset(e); return 1; }
    for (int sequence = 0; sequence < e->sequences; sequence++) {
        const float * pooled = llama_get_embeddings_seq(e->context, sequence);
        if (!pooled) { set_error(error, error_size, "no pooled embedding for input %d", sequence); reset(e); return 1; }
        memcpy(out + (size_t) sequence * e->embedding_size, pooled, sizeof(float) * (size_t) e->embedding_size);
    }
    reset(e);
    return 0;
}

// <bos> text <eos>, cut to max_tokens the way transformers cuts (keeping both special tokens). Returns the count.
static int tokenize(recall_embedder * e, const char * text, llama_token ** tokens_out) {
    int32_t length = (int32_t) strlen(text);
    int32_t capacity = length + 8;
    llama_token * tokens = malloc(sizeof(llama_token) * (size_t) capacity);
    int32_t count = llama_tokenize(e->vocab, text, length, tokens, capacity, true, true);
    if (count < 0) {
        capacity = -count;
        tokens = realloc(tokens, sizeof(llama_token) * (size_t) capacity);
        count = llama_tokenize(e->vocab, text, length, tokens, capacity, true, true);
    }
    if (count < 0) count = 0;
    if (count > e->max_tokens) {
        count = e->max_tokens;
        tokens[count - 1] = llama_vocab_eos(e->vocab);
    }
    *tokens_out = tokens;
    return count;
}

// Embeds `count` texts (already prompted) into `out` (count x embedding_size). Returns 0 on success.
int recall_embed_texts(recall_embedder * e, const char * const * texts, int count, float * out,
                       char * error, size_t error_size) {
    reset(e);
    int done = 0;   // inputs written to `out`
    for (int i = 0; i < count; i++) {
        llama_token * tokens;
        int n = tokenize(e, texts[i], &tokens);
        if (e->sequences == MAX_SEQUENCES || e->tokens + n > e->max_tokens) {
            int ran = e->sequences;
            if (run(e, out + (size_t) done * e->embedding_size, error, error_size)) { free(tokens); return 1; }
            done += ran;
        }
        begin(e);
        for (int t = 0; t < n; t++) {
            if (!place(e, llama_batch_ext_add_token(e->batch, e->sequences - 1, tokens[t]))) {
                set_error(error, error_size, "batch full");
                free(tokens);
                reset(e);
                return 1;
            }
        }
        free(tokens);
    }
    if (e->sequences > 0 && run(e, out + (size_t) done * e->embedding_size, error, error_size)) return 1;
    return 0;
}

// Reads an image or audio file with mtmd's helpers (stb_image, miniaudio at 16 kHz), resizes images for `budget` and
// cuts audio at 30 s. NULL on failure (with the reason in `error`).
static mtmd_bitmap * load(recall_embedder * e, const char * path, int image, int budget, char * error, size_t size) {
    struct mtmd_helper_bitmap_wrapper wrapper =
        mtmd_helper_bitmap_init_from_file(e->media, path, false, mtmd_helper_init_opt_default());
    if (wrapper.video_ctx) {
        if (wrapper.bitmap) mtmd_bitmap_free(wrapper.bitmap);
        mtmd_helper_video_free(wrapper.video_ctx);
        set_error(error, size, "video goes in as frames and sound, not as a file: %s", path);
        return NULL;
    }
    mtmd_bitmap * bitmap = wrapper.bitmap;
    if (!bitmap) { set_error(error, size, "can't read %s", path); return NULL; }
    if (mtmd_bitmap_is_audio(bitmap) == (image != 0)) {
        mtmd_bitmap_free(bitmap);
        set_error(error, size, "not %s: %s", image ? "an image" : "audio", path);
        return NULL;
    }
    if (image) {
        int width = (int) mtmd_bitmap_get_nx(bitmap), height = (int) mtmd_bitmap_get_ny(bitmap);
        int target_height, target_width;
        if (!recall_target_size(height, width, budget, &target_height, &target_width)) {
            mtmd_bitmap_free(bitmap);
            set_error(error, size, "image too thin to embed: %s", path);
            return NULL;
        }
        if (target_height == height && target_width == width) return bitmap;
        unsigned char * pixels = malloc((size_t) target_width * target_height * 3);
        recall_resize_bicubic(mtmd_bitmap_get_data(bitmap), width, height, target_width, target_height, pixels);
        mtmd_bitmap_free(bitmap);
        mtmd_bitmap * resized = mtmd_bitmap_init((uint32_t) target_width, (uint32_t) target_height, pixels);
        free(pixels);
        if (!resized) set_error(error, size, "can't resize %s", path);
        return resized;
    }
    size_t samples = mtmd_bitmap_get_n_bytes(bitmap) / sizeof(float);
    size_t limit = (size_t) 30 * (size_t) mtmd_get_audio_sample_rate(e->media);
    if (samples <= limit) return bitmap;
    mtmd_bitmap * prefix = mtmd_bitmap_init_from_audio(limit, (const float *) mtmd_bitmap_get_data(bitmap));
    mtmd_bitmap_free(bitmap);
    if (!prefix) set_error(error, size, "can't cut %s at 30 s", path);
    return prefix;
}

// One image (`image` = 1, with its token budget) or audio file's vector into `out`. Returns 0 on success.
int recall_embed_media(recall_embedder * e, const char * path, int image, int budget, float * out,
                       char * error, size_t error_size) {
    if (!e->media) { set_error(error, error_size, "this embedder was started text-only"); return 1; }
    mtmd_bitmap * bitmap = load(e, path, image, budget, error, error_size);
    if (!bitmap) return 1;
    mtmd_input_chunks * chunks = mtmd_input_chunks_init();
    mtmd_input_text text = { mtmd_default_marker(), strlen(mtmd_default_marker()), true, true };
    const mtmd_bitmap * bitmaps[1] = { bitmap };
    int32_t status = mtmd_tokenize(e->media, chunks, &text, bitmaps, 1);
    int result = 1;
    if (status != 0) {
        set_error(error, error_size, "can't tokenize %s (%d)", path, status);
        goto done;
    }
    reset(e);
    begin(e);
    for (size_t index = 0; index < mtmd_input_chunks_size(chunks); index++) {
        const mtmd_input_chunk * chunk = mtmd_input_chunks_get(chunks, index);
        if (mtmd_input_chunk_get_type(chunk) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
            size_t count = 0;
            const llama_token * tokens = mtmd_input_chunk_get_tokens_text(chunk, &count);
            for (size_t t = 0; tokens && t < count; t++) {
                if (!place(e, llama_batch_ext_add_token(e->batch, e->sequences - 1, tokens[t]))) {
                    set_error(error, error_size, "batch full");
                    reset(e);
                    goto done;
                }
            }
        } else {
            float * rows;
            if (mtmd_encode_chunk(e->media, chunk) != 0 || !(rows = mtmd_get_output_embd(e->media))) {
                set_error(error, error_size, "the encoder failed on %s", path);
                reset(e);
                goto done;
            }
            size_t count = mtmd_input_chunk_get_n_tokens(chunk);
            for (size_t row = 0; row < count; row++) {
                struct llama_embd embd = { rows + row * (size_t) e->row_size, 1, (size_t) e->row_size };   // copied
                if (!place(e, llama_batch_ext_add_embd(e->batch, e->sequences - 1, embd))) {
                    set_error(error, error_size, "batch full");
                    reset(e);
                    goto done;
                }
            }
        }
    }
    result = run(e, out, error, error_size);
done:
    mtmd_input_chunks_free(chunks);
    mtmd_bitmap_free(bitmap);
    return result;
}
