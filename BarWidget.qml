import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

BarWidget {
  id: root
  moduleName: "jmcanana.pressure"

  readonly property string stateDir: (Quickshell.env("HOME") || "") + "/.local/state/jmcanana.pressure"
  property string syncState: "idle"
  property string concern: "none"

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
  }

  function applyStatus(raw) {
    try {
      var doc = JSON.parse(raw || "{}")
      root.syncState = doc.state || "idle"
      root.concern = doc.concern || "none"
    } catch (error) {
      root.syncState = "idle"
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()

  FileView {
    path: root.stateDir + "/status.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.applyStatus(text())
    onFileChanged: reload()
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    active: root.concern === "severe" || root.concern === "watch" || root.syncState === "error"
    text: {
      if (root.syncState === "draining" || root.syncState === "connecting") return "Sync"
      if (root.syncState === "ready") return "Ready"
      if (root.syncState === "seen") return "New"
      return "BP"
    }
    tooltipText: "Pressure. Readings stay inside the panel."
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
    }
  }
}
