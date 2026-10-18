import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateDir: home + "/.local/state/jmcanana.pressure"
  readonly property string dataDir: home + "/.local/share/jmcanana.pressure"
  readonly property string pluginDir: {
    var folder = decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, ""))
    return folder.endsWith("/") ? folder : folder + "/"
  }
  readonly property bool wanted: true

  Process {
    id: sync
    workingDirectory: root.pluginDir
    command: [root.pluginDir + "pressure/run", "--state", root.stateDir, "--data", root.dataDir]
    running: true
    onRunningChanged: {
      if (!sync.running && root.wanted) restart.restart()
    }
  }

  Timer {
    id: restart
    interval: 4000
    repeat: false
    onTriggered: {
      if (root.wanted && !sync.running) sync.running = true
    }
  }

}
