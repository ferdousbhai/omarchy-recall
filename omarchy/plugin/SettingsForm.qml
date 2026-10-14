import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons

ScrollView {
  id: form
  signal saveRequested(var settings)
  property var config: ({})
  function load(settings) {
    config = settings
    folders.text = (settings.roots || []).join("\n")
    codeFolders.text = (settings.code_roots || []).join("\n")
    excluded.text = (settings.excluded_folders || []).join("\n")
    codeExcluded.text = (settings.code_excluded || []).join("\n")
    types.text = (settings.excluded_types || []).join(", ")
    editor.currentIndex = Math.max(0, editor.model.indexOf(settings.code_editor || ""))
    battery.checked = settings.index_on_battery !== false
    idle.value = settings.encoder_idle_seconds || 120
  }
  function save() { form.saveRequested({roots: form.lines(folders.text), code_roots: form.lines(codeFolders.text), excluded_folders: form.lines(excluded.text), code_excluded: form.lines(codeExcluded.text), excluded_types: types.text.split(/[,\s]+/).filter(function(s) { return s.length > 0 }), code_editor: editor.currentText, index_on_battery: battery.checked, encoder_idle_seconds: idle.value}) }
  function lines(text) { return text.split(/\n/).map(function(s) { return s.trim() }).filter(function(s) { return s.length > 0 }) }
  clip: true
  ColumnLayout {
    width: form.availableWidth
    spacing: 10
    Text { text: "Search folders"; color: Color.menu.text; font.bold: true }
    Text { text: "One absolute folder path per line. Only selected folders are indexed."; color: Color.menu.text; opacity: 0.7; wrapMode: Text.Wrap; Layout.fillWidth: true }
    TextArea { id: folders; Layout.fillWidth: true; placeholderText: "/home/you/Documents\n/home/you/Pictures"; selectByMouse: true }
    Text { text: "Code repositories"; color: Color.menu.text; font.bold: true }
    TextArea { id: codeFolders; Layout.fillWidth: true; placeholderText: "/home/you/projects"; selectByMouse: true }
    Text { text: "Excluded folders"; color: Color.menu.text }
    TextArea { id: excluded; Layout.fillWidth: true; placeholderText: "Absolute paths, one per line"; selectByMouse: true }
    Text { text: "Excluded code folders"; color: Color.menu.text }
    TextArea { id: codeExcluded; Layout.fillWidth: true; placeholderText: "Absolute paths, one per line"; selectByMouse: true }
    Text { text: "Excluded file extensions"; color: Color.menu.text }
    TextField { id: types; Layout.fillWidth: true; placeholderText: "heic, csv"; selectByMouse: true }
    RowLayout {
      Text { text: "Code editor"; color: Color.menu.text }
      ComboBox { id: editor; model: ["", "nvim", "code", "codium", "cursor", "windsurf", "zed", "subl", "pycharm", "idea", "clion", "goland", "webstorm", "rustrover", "kate", "gnome-text-editor", "gedit"] }
    }
    CheckBox { id: battery; text: "Allow initial indexing on battery" }
    RowLayout {
      Text { text: "Release idle search model after (seconds)"; color: Color.menu.text }
      SpinBox { id: idle; from: 1; to: 86400; editable: true }
    }
    Text { text: "Index: " + (form.config.index_directory || "") + "\nModels: " + (form.config.models_directory || ""); color: Color.menu.text; opacity: 0.65; wrapMode: Text.Wrap; Layout.fillWidth: true; textFormat: Text.PlainText }
    Text { text: "OCR is optional. Run recall ocr to add text recognition for screenshots and scanned pages."; color: Color.menu.text; opacity: 0.7; wrapMode: Text.Wrap; Layout.fillWidth: true }

  }
}
