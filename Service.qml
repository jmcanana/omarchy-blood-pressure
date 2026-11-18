import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateDir: root.xdgDir("XDG_STATE_HOME", "/.local/state") + "/jmcanana.blood-pressure"
  readonly property string dataDir: root.xdgDir("XDG_DATA_HOME", "/.local/share") + "/jmcanana.blood-pressure"
  readonly property string pluginDir: {
    var folder = decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, ""))
    return folder.endsWith("/") ? folder : folder + "/"
  }

  // The XDG base directory rule, as pressure/run and the sidecar apply it: an absolute value or the default.
  function xdgDir(name, fallback) {
    var value = Quickshell.env(name) || ""
    return value.startsWith("/") ? value : root.home + fallback
  }

  // Exit 3 is a lost scan (pressure/sidecar.py): restart it in 4 s, as before. Any other exit backs off,
  // doubling from 4 s to 5 min, so a sidecar that cannot start does not loop every 4 s for ever.
  readonly property int scanLostExit: 3
  readonly property int firstDelay: 4000
  readonly property int longestDelay: 300000
  readonly property int steadyAfter: 60000
  property int delay: root.firstDelay
  property double startedAt: 0

  function nextDelay(exitCode, upFor) {
    if (exitCode === root.scanLostExit || upFor >= root.steadyAfter) root.delay = root.firstDelay
    var wait = root.delay
    if (exitCode !== root.scanLostExit) root.delay = Math.min(root.delay * 2, root.longestDelay)
    return wait
  }

  Process {
    id: sync
    workingDirectory: root.pluginDir
    command: [root.pluginDir + "pressure/run", "--state", root.stateDir, "--data", root.dataDir]
    running: true
    onRunningChanged: {
      if (sync.running) root.startedAt = Date.now()
    }
    onExited: function(exitCode) {
      restart.interval = root.nextDelay(exitCode, Date.now() - root.startedAt)
      restart.restart()
    }
  }

  Timer {
    id: restart
    interval: root.firstDelay
    repeat: false
    onTriggered: {
      if (!sync.running) sync.running = true
    }
  }

}
