//! Text the way keyword search keeps it and matches it, so that a word is found however it was typed or extracted;
//! and what can go wrong with the text read from a PDF page.

use std::ops::Range;

use regex::Regex;
use std::sync::LazyLock;
use unicode_general_category::{get_general_category, GeneralCategory as Category};
use unicode_normalization::char::is_combining_mark;
use unicode_normalization::UnicodeNormalization;

/// Bengali, Hindi and the other Indic scripts.
pub const INDIC: Range<u32> = 0x0900..0x0E00;
pub const ARABIC: Range<u32> = 0x0600..0x0700;

/// NFKC (Arabic presentation forms and ligatures become plain letters, full-width becomes ASCII, and Bengali's two
/// spellings of "ো" become one), invisible formatting characters dropped (Bengali's zero-width joiners, direction
/// marks), and Arabic the way people type it: no tatweel, no short-vowel marks, one alef.
pub fn normalized(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.nfkc() {
        match ch as u32 {
            0x200B => out.push(' '), // zero-width space: a word break (Thai, Khmer)
            0x00AD | 0x061C | 0x200C..=0x200F | 0x202A..=0x202E | 0x2060..=0x2064 | 0x2066..=0x2069 | 0xFEFF => {}
            0x0640 | 0x064B..=0x065F | 0x0670 => {} // tatweel; harakat and the dagger alef
            0x0622 | 0x0623 | 0x0625 | 0x0671 => out.push('\u{0627}'), // alef with hamza or madda, wasla → alef
            _ => out.push(ch),
        }
    }
    out
}

/// True for words of scripts that attach endings to words (Bengali, Hindi, Arabic): keyword search matches them as
/// prefixes, so "কুমির" finds "কুমিরও" and "مدرس" finds "مدرسة".
pub fn inflects(word: &str) -> bool {
    word.chars().any(|ch| INDIC.contains(&(ch as u32)) || ARABIC.contains(&(ch as u32)))
}

fn is_letter_or_mark(ch: char) -> bool {
    use Category::*;
    matches!(
        get_general_category(ch),
        UppercaseLetter | LowercaseLetter | TitlecaseLetter | ModifierLetter | OtherLetter | NonspacingMark
            | SpacingMark | EnclosingMark
    )
}

/// Letters, marks and numbers: what a word is made of (Bengali vowel signs stay inside their words).
pub fn is_word_char(ch: char) -> bool {
    use Category::*;
    is_letter_or_mark(ch) || matches!(get_general_category(ch), DecimalNumber | LetterNumber | OtherNumber)
}

/// Whether text says something in words: at least a fifth of what isn't space is letters (marks count, such as
/// Bengali vowel signs). A data dump or a table of numbers isn't, and gets keyword search only: a meaning vector of
/// digits matches nothing anyone would describe.
pub fn has_words(text: &str) -> bool {
    let (mut letters, mut others) = (0usize, 0usize);
    for ch in text.chars().filter(|ch| !ch.is_whitespace()) {
        if is_letter_or_mark(ch) {
            letters += 1;
        } else {
            others += 1;
        }
    }
    letters > 0 && letters * 4 >= others
}

/// One character as keyword search compares it: normalized, lowercase, and without the accents of Latin, Greek or
/// Cyrillic letters ("é" is "e"); other scripts' marks are part of their letters and stay.
fn fold(ch: char) -> String {
    let lower = normalized(&ch.to_string()).to_lowercase();
    let mut out = String::new();
    for piece in lower.chars() {
        let decomposed: Vec<char> = piece.to_string().nfd().collect();
        if decomposed.first().is_some_and(|base| (*base as u32) < 0x0530) {
            out.extend(decomposed.into_iter().filter(|c| !is_combining_mark(*c)));
        } else {
            out.push(piece);
        }
    }
    out
}

/// `text` folded one character at a time, remembering which character of the source each folded one came from.
struct Folded {
    chars: Vec<char>,
    /// For each folded character, the byte range of the source character it came from.
    origin: Vec<Range<usize>>,
}

impl Folded {
    fn new(source: &str) -> Self {
        let (mut chars, mut origin) = (Vec::new(), Vec::new());
        for (at, ch) in source.char_indices() {
            for piece in fold(ch).chars() {
                chars.push(piece);
                origin.push(at..at + ch.len_utf8());
            }
        }
        Self { chars, origin }
    }
}

/// Where `word` is in `text` as keyword search would find it (normalized, ignoring case and accents), as byte ranges of
/// the text as it's written: whole words, and for a word of a language that attaches endings, the whole word it
/// starts ("কুমির" marks all of "কুমিরের"). It never splits a letter from its vowel sign.
pub fn matches(word: &str, text: &str) -> Vec<Range<usize>> {
    let needle: Vec<char> = word.chars().flat_map(|ch| fold(ch).chars().collect::<Vec<_>>()).collect();
    if needle.is_empty() {
        return Vec::new();
    }
    let folded = Folded::new(text);
    let prefix = inflects(word);
    let mut found: Vec<Range<usize>> = Vec::new();
    let mut from = 0;
    while found.len() < 500 && from + needle.len() <= folded.chars.len() {
        let Some(offset) = folded.chars[from..].windows(needle.len()).position(|window| window == needle.as_slice())
        else {
            break;
        };
        let at = from + offset;
        from = at + needle.len();
        let mut range = folded.origin[at].start..folded.origin[at + needle.len() - 1].end;
        if text[..range.start].chars().next_back().is_some_and(is_word_char) {
            continue;
        }
        if text[range.end..].chars().next().is_some_and(is_word_char) {
            if !prefix {
                continue;
            }
            let rest = &text[range.end..];
            let extra: usize = rest.chars().take_while(|ch| is_word_char(*ch)).map(char::len_utf8).sum();
            range.end += extra;
        }
        if found.last().is_some_and(|last| last.end > range.start) {
            continue;
        }
        found.push(range);
    }
    found
}

static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());

/// One line from `text` around the first of `terms` it has, the terms marked « » as SQLite's snippet() marks them:
/// the excerpt of a keyword match, from the text as it's written. None when none of the terms is in it.
pub fn snippet(text: &str, terms: &[String], words: usize) -> Option<String> {
    let mut marks: Vec<Range<usize>> = terms.iter().flat_map(|term| matches(term, text)).collect();
    marks.sort_by_key(|mark| mark.start);
    let first = marks.first()?.clone();
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let index_of = |byte: usize| chars.iter().position(|(at, _)| *at >= byte).unwrap_or(chars.len());
    // About `words` words, starting a few before the first match.
    let (mut start, mut before) = (index_of(first.start), 0);
    let first_char = start;
    while start > 0 && before < words / 3 {
        start -= 1;
        if chars[start].1.is_whitespace() && start < first_char && !chars[start + 1].1.is_whitespace() {
            before += 1;
        }
    }
    if start > 0 || chars.get(start).is_some_and(|(_, ch)| ch.is_whitespace()) {
        start += 1;
    }
    let (mut end, mut after) = (index_of(first.end), 0);
    while end < chars.len() && after < words - before {
        if chars[end].1.is_whitespace() {
            after += 1;
        }
        end += 1;
    }
    let byte = |index: usize| chars.get(index).map(|(at, _)| *at).unwrap_or(text.len());
    let (start_byte, end_byte) = (byte(start), byte(end));
    let mut out = if start > 0 { "…".to_string() } else { String::new() };
    let mut cursor = start_byte;
    for mark in &marks {
        if mark.start < start_byte || mark.end > end_byte || mark.start < cursor {
            continue;
        }
        out.push_str(&text[cursor..mark.start]);
        out.push('«');
        out.push_str(&text[mark.clone()]);
        out.push('»');
        cursor = mark.end;
    }
    out.push_str(&text[cursor..end_byte]);
    if end < chars.len() {
        out.push('…');
    }
    Some(SPACES.replace_all(&out, " ").trim_matches([' ', '\t']).to_string())
}

/// What's wrong with the text read from a PDF page, when the page's text layer can't be used as it is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PageTextProblem {
    /// Bengali or Hindi read in glyph order: vowel signs ahead of their consonants, conjuncts lost ("চুক্তিপত্র" reads
    /// "চিপ"). PDF readers get most Bengali PDFs this way, whatever made them. The page is used as a picture instead
    /// (the OCR doesn't read Bengali).
    GlyphOrder,
    /// Arabic letters lost where the font drew a lam ligature: the article's lam goes with the ligature, leaving a
    /// bare alef before a "?" or "." ("الأردن" reads "ا?ردن"). The page is read again by OCR. A bare alef, not أ:
    /// "أ.د." (Prof. Dr.) and "ذ.م.م" (LLC) are real abbreviations.
    LostLetters,
}

impl PageTextProblem {
    pub fn check(text: &str) -> Option<Self> {
        let (mut vowel_signs, mut misplaced, mut lost) = (0usize, 0usize, 0usize);
        let (mut previous, mut before_previous) = (' ', ' ');
        for ch in text.nfc() {
            if is_indic_vowel_sign(ch as u32) {
                vowel_signs += 1;
                if !is_indic_consonant_or_nukta(previous as u32) {
                    misplaced += 1;
                }
            }
            if before_previous == '\u{0627}'
                && ['?', '.', '\u{FFFD}'].contains(&previous)
                && ARABIC.contains(&(ch as u32))
                && ch.is_alphabetic()
            {
                lost += 1;
            }
            before_previous = previous;
            previous = ch;
        }
        if vowel_signs >= 10 && misplaced * 20 >= vowel_signs {
            return Some(Self::GlyphOrder);
        }
        (lost >= 2).then_some(Self::LostLetters)
    }
}

/// Dependent vowel signs of Bengali and Devanagari: in a real word, each follows a consonant (or its nukta).
fn is_indic_vowel_sign(value: u32) -> bool {
    matches!(value, 0x09BE..=0x09C4 | 0x09C7 | 0x09C8 | 0x09CB | 0x09CC | 0x09D7 | 0x09E2 | 0x09E3)
        || matches!(value, 0x093A | 0x093B | 0x093E..=0x094C | 0x094E | 0x094F | 0x0955..=0x0957 | 0x0962 | 0x0963)
}

fn is_indic_consonant_or_nukta(value: u32) -> bool {
    matches!(value, 0x0995..=0x09B9 | 0x09BC | 0x09CE | 0x09DC | 0x09DD | 0x09DF | 0x09F0 | 0x09F1)
        || matches!(value, 0x0915..=0x0939 | 0x093C | 0x0958..=0x095F | 0x0979..=0x097F)
}

/// How long text is, in characters as people see them (a letter with its marks is one).
pub fn length_chars(text: &str) -> usize {
    use unicode_segmentation::UnicodeSegmentation;
    text.graphemes(true).count()
}

/// One line, at most `limit` characters: for result lists.
pub fn excerpt(text: &str, limit: usize) -> String {
    let line = SPACES.replace_all(text, " ");
    let line = line.trim_matches([' ', '\t']);
    if length_chars(line) > limit {
        prefix(line, limit).to_string() + "…"
    } else {
        line.to_string()
    }
}

/// The first `limit` characters of `text`, as people see them.
pub fn prefix(text: &str, limit: usize) -> &str {
    use unicode_segmentation::UnicodeSegmentation;
    match text.grapheme_indices(true).nth(limit) {
        Some((at, _)) => &text[..at],
        None => text,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn found(word: &str, text: &str) -> Vec<String> {
        matches(word, text).into_iter().map(|range| text[range].to_string()).collect()
    }

    #[test]
    fn normalized_folds_what_is_typed_differently() {
        assert_eq!(normalized("الْمَكْتَبَةَ"), "المكتبة");
        assert_eq!(normalized("إلى الأردن"), "الى الاردن");
        assert_eq!(normalized("مـــرحبا"), "مرحبا");
        assert_eq!(normalized("\u{FEFB}"), "\u{0644}\u{0627}");
        assert_eq!(normalized("\u{FB01}le"), "file");
        assert_eq!(normalized("র\u{200D}্যাব"), "র্যাব");
        assert_eq!(normalized("\u{09C7}\u{09BE}"), "\u{09CB}");
        assert_eq!(normalized("a\u{200B}b"), "a b");
    }

    #[test]
    fn matches_are_found_as_keyword_search_finds_them_but_shown_as_written() {
        assert_eq!(found("الاردن", "زرنا مدينة البتراء الأثرية في جنوب الأردن."), ["الأردن"]);
        assert_eq!(found("المكتبة", "إِنَّ الْمَكْتَبَةَ تُغْلَقُ"), ["الْمَكْتَبَةَ"]);
        assert_eq!(found("কুমির", "একটি বড় কুমিরের ছবি"), ["কুমিরের"]);
        assert_eq!(found("day", "Sunday trip: day two"), ["day"]);
        assert_eq!(found("cafe", "Le Café du port"), ["Café"]);
        let turkish = "İ".repeat(60) + " son kelime";
        assert_eq!(found("kelime", &turkish), ["kelime"]);
    }

    #[test]
    fn snippets_quote_the_text_as_written() {
        let text = "قَالَ مُحَمَّدٌ إِنَّ الْمَكْتَبَةَ الْعَامَّةَ تُغْلَقُ فِي السَّاعَةِ الثَّامِنَةِ مَسَاءً يَوْمَ الْخَمِيسِ";
        let snippet = snippet(text, &["المكتبة".to_string()], 12).unwrap();
        assert!(snippet.contains("«الْمَكْتَبَةَ»"), "{snippet}");
        assert_eq!(super::snippet(text, &["كلمة".to_string()], 12), None);
        let english = "The tenant may keep one cat with the landlord's written consent, said the lease.";
        let line = super::snippet(english, &["cat".to_string()], 12).unwrap();
        assert!(line.contains("«cat»") && line.starts_with('…') || line.starts_with("The"), "{line}");
    }

    #[test]
    fn garbled_page_text_is_noticed() {
        let glyph_order = "বািড় ভাড়ার চিপ ধানমি ৭ নর রােডর িতন বডরেমর াট মািসক ২৫,০০০ টাকা ভাড়ায় দওয়া হেলা। ভাড়ায়া দুই মােসর";
        let bengali = "বাড়ি ভাড়ার চুক্তিপত্র। ধানমন্ডি ৭ নম্বর রোডের তিন বেডরুমের ফ্ল্যাটটি মাসিক ২৫,০০০ টাকা ভাড়ায় দেওয়া হলো।";
        let lost = "ارتفعت ا.بيعات بنسبة خمسة عشر با.ئة في الربع الثالث. زرنا مدينة البتراء ا?ثرية في جنوب ا?ردن.";
        let arabic = "ارتفعت المبيعات بنسبة خمسة عشر بالمئة في الربع الثالث. زرنا مدينة البتراء الأثرية في جنوب الأردن.";
        assert_eq!(PageTextProblem::check(glyph_order), Some(PageTextProblem::GlyphOrder));
        assert_eq!(PageTextProblem::check(bengali), None);
        assert_eq!(PageTextProblem::check(lost), Some(PageTextProblem::LostLetters));
        assert_eq!(PageTextProblem::check(arabic), None);
        assert_eq!(PageTextProblem::check("Revenue grew 20%. See p.4?No. Done."), None);
        assert_eq!(PageTextProblem::check("أ.د.محمد علي، أستاذ في الجامعة. د.خالد و د.سارة من شركة ذ.م.م للتجارة"), None);
        assert_eq!(PageTextProblem::check(&"\u{F0B7} Item one of the slide\n".repeat(6)), None);
    }

    #[test]
    fn words_against_numbers() {
        assert!(has_words("The tenant may keep one cat."));
        assert!(has_words("বাড়ি ভাড়ার চুক্তিপত্র"));
        assert!(!has_words("12.5, 13.1, 14.0, 15.2, 16.8, 17.3, 18.9"));
    }
}
