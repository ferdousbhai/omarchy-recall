import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons

Item {
  id: root
  property var shell: null
  property var manifest: null
  property string omarchyPath: ""
  property bool showSettings: false
  property int settingsSave: -1
  property bool opened: false
  property var hits: []
  property int serial: 0
  property int latestSearch: -1
  property int selected: 0
  property string status: "Starting search service…"
  property string notice: ""
  property bool paused: false
  property bool searching: false

  function send(op, data) {
    var message = data || {}
    message.op = op
    message.id = ++serial
    transport.write(JSON.stringify(message) + "\n")
    return message.id
  }
  function open(payload) {
    opened = true
    showSettings = false
    try {
      var options = JSON.parse(payload || "{}")
      showSettings = options.settings === true
      if (options.query !== undefined) query.text = String(options.query)
    } catch (error) {}
    if (showSettings) send("config")
    else send("prewarm")
    send("status")
    Qt.callLater(function() { if (root.showSettings) settingsForm.forceActiveFocus(); else query.forceActiveFocus() })
  }
  function close() {
    opened = false
    debounce.stop()
    send("release")
  }
  function search() {
    if (!query.text.trim()) { hits = []; searching = false; return }
    searching = true
    latestSearch = send("search", {query: query.text})
  }
  function activate(reveal) {
    if (hits.length && selected >= 0 && selected < hits.length) {
      send(reveal ? "reveal" : "open", {hit: hits[selected]})
      close()
    }
  }
  Process {
    id: transport
    command: [Quickshell.env("HOME") + "/.local/bin/recall-client"]
    running: true
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) {
        try {
          var reply = JSON.parse(line)
          if (reply.error) { root.notice = reply.error; root.searching = false; return }
          if (reply.config !== undefined) settingsForm.load(reply.config)
          if (reply.id === root.settingsSave) { root.showSettings = false; root.notice = "Settings saved" }
          if (reply.line !== undefined) { root.status = reply.line; root.paused = reply.paused }
          if (reply.id === root.latestSearch && !reply.skipped) {
            root.hits = reply.hits || []
            root.selected = 0
            root.searching = false
            root.notice = reply.encoder_error ? "Exact matches · " + reply.encoder_error : reply.meaning ? "Semantic + exact matches" : "Exact matches · semantic encoder warming up"
          }
        } catch (error) { root.notice = "Could not read search response" }
      }
    }
    onExited: { root.notice = "Search connection stopped"; reconnect.restart() }
  }
  Timer { id: reconnect; interval: 2000; onTriggered: transport.running = true }
  Timer { id: debounce; interval: 250; onTriggered: root.search() }
  Timer { interval: 3000; running: root.opened; repeat: true; onTriggered: root.send("status") }
  PanelWindow {
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "recall-for-omarchy"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    Rectangle { anchors.fill: parent; color: "#66000000"; MouseArea { anchors.fill: parent; onClicked: root.close() } }
    Rectangle {
      anchors.centerIn: parent
      width: Math.min(860, parent.width - 64)
      height: Math.min(680, parent.height - 64)
      color: Color.menu.background
      border.color: Color.menu.border
      border.width: 2
      radius: 14
      MouseArea { anchors.fill: parent; onClicked: query.forceActiveFocus() }
      ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 12
        RowLayout {
          Layout.fillWidth: true
          Text { text: "Recall"; color: Color.menu.text; font.pixelSize: 23; font.bold: true }
          Item { Layout.fillWidth: true }
          Button { text: root.paused ? "Resume indexing" : "Pause indexing"; onClicked: { root.send("pause", {paused: !root.paused}); root.send("status") } }
          Button { text: root.showSettings ? "Back to search" : "Settings"; onClicked: { root.showSettings = !root.showSettings; if (root.showSettings) root.send("config"); else query.forceActiveFocus() } }
        }
        TextField {
          id: query
          visible: !root.showSettings
          Layout.fillWidth: true
          placeholderText: "Describe a file, scene, or moment…  ·  code: search by intent"
          font.pixelSize: 18
          onTextChanged: { root.notice = ""; debounce.restart() }
          Keys.onEscapePressed: root.close()
          Keys.onDownPressed: { root.selected = Math.min(root.hits.length - 1, root.selected + 1); list.positionViewAtIndex(root.selected, ListView.Contain) }
          Keys.onUpPressed: { root.selected = Math.max(0, root.selected - 1); list.positionViewAtIndex(root.selected, ListView.Contain) }
          Keys.onReturnPressed: function(event) { root.activate((event.modifiers & Qt.ControlModifier) !== 0) }
          Keys.onEnterPressed: function(event) { root.activate((event.modifiers & Qt.ControlModifier) !== 0) }
        }
        Text { text: root.status; color: Color.menu.text; opacity: 0.7; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
        SettingsForm {
          id: settingsForm
          visible: root.showSettings
          Layout.fillWidth: true
          Layout.fillHeight: true
          onSaveRequested: function(settings) { root.settingsSave = root.send("configure", {config: settings}) }
          Keys.onEscapePressed: root.close()
        }
        ListView {
          id: list
          visible: !root.showSettings
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          spacing: 6
          model: root.hits
          delegate: Rectangle {
            required property var modelData
            required property int index
            width: list.width
            height: 94
            radius: 8
            color: index === root.selected ? Color.menu.selectedBackground : "transparent"
            RowLayout {
              anchors.fill: parent
              anchors.margins: 10
              spacing: 12
              Image {
                Layout.preferredWidth: 68
                Layout.preferredHeight: 68
                source: ["image", "screenshot"].indexOf(modelData.kind) >= 0 ? "file://" + encodeURI(modelData.path).replace(/#/g, "%23") : ""
                sourceSize.width: 136
                sourceSize.height: 136
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                visible: source !== ""
              }
              ColumnLayout {
                Layout.fillWidth: true
                spacing: 4
                Text { text: modelData.name + (modelData.location ? "  ·  " + modelData.location : ""); color: Color.menu.text; font.bold: true; font.pixelSize: 15; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText }
                Text { text: modelData.excerpt || modelData.kind; color: Color.menu.text; opacity: 0.85; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight; textFormat: Text.PlainText }
                Text { text: modelData.folder; color: Color.menu.text; opacity: 0.55; font.pixelSize: 11; Layout.fillWidth: true; elide: Text.ElideMiddle; textFormat: Text.PlainText }
              }
            }
            MouseArea { anchors.fill: parent; onClicked: { root.selected = index; query.forceActiveFocus() }
              onDoubleClicked: { root.selected = index; root.activate(false) } }
          }
          Text {
            anchors.centerIn: parent
            visible: !root.hits.length
            text: root.searching ? "Searching…" : query.text.trim() ? "No matches yet" : "Search your files by what’s in them.\nUse code: for your projects."
            color: Color.menu.text
            opacity: 0.65
            horizontalAlignment: Text.AlignHCenter
          }
        }
        Text { text: root.notice; color: Color.menu.text; opacity: 0.75; font.pixelSize: 12; Layout.fillWidth: true; elide: Text.ElideRight }
        Button { visible: root.showSettings; text: "Save settings"; onClicked: settingsForm.save() }
        Text { visible: !root.showSettings; text: "↑ ↓ Select    Enter Open    Ctrl+Enter Reveal    Esc Close     ·     Everything stays on this computer"; color: Color.menu.text; opacity: 0.6; font.pixelSize: 11 }
      }
    }
  }
}
