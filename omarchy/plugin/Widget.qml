import QtQuick
import qs.Ui

BarWidget {
  id: root
  moduleName: "ferdous.recall"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰍉"
    horizontalMargin: 8
    onPressed: function(mouseButton) {
      if (root.bar) root.bar.run("recall-shell shell toggle ferdous.recall '{}'")
    }
  }
}
