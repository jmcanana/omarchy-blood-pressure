import QtQuick
import qs.Commons

// Header pill: waiting, measuring, syncing, synced or error, each with its own glyph and motion.
Rectangle {
  id: root
  property string linkState: "idle"
  property var lastSync: null
  property color ink: Color.foreground
  property color ink2: Color.muted
  property string fontFamily: Style.font.family

  readonly property string mode: {
    if (linkState === "error") return "error"
    if (linkState === "connecting" || linkState === "draining") return "syncing"
    if (linkState === "ready") return "measuring"
    if (lastSync && justSynced) return "synced"
    return "waiting"
  }
  property bool justSynced: false
  property string previous: ""

  onLinkStateChanged: {
    if ((previous === "draining" || previous === "connecting") && linkState === "scanning") {
      justSynced = true
      settle.restart()
    }
    previous = linkState
  }

  Timer {
    id: settle
    interval: 5 * 60 * 1000
    onTriggered: root.justSynced = false
  }

  readonly property string label: {
    if (mode === "error") return "Sync failed · retries next time"
    if (mode === "syncing") return "Syncing"
    if (mode === "measuring") return "Measuring · waiting for light"
    if (mode === "synced") return "Synced " + lastSync.stored + " · " + lastSync.at
    return lastSync ? "Waiting · last sync " + lastSync.at : "Waiting for a measurement"
  }

  implicitWidth: row.implicitWidth + Style.space(20)
  implicitHeight: Style.space(26)
  radius: height / 2
  color: mode === "error" ? Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.10) : Qt.rgba(ink.r, ink.g, ink.b, 0.06)
  border.width: 1
  border.color: mode === "error" ? Color.urgent : Qt.rgba(ink.r, ink.g, ink.b, 0.16)

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(7)

    Item {
      width: Style.space(12)
      height: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter

      Canvas {
        id: glyph
        anchors.fill: parent
        property real turn: 0
        property real halo: 1
        onTurnChanged: requestPaint()
        onHaloChanged: requestPaint()
        Connections {
          target: root
          function onModeChanged() { glyph.requestPaint() }
        }
        onPaint: {
          var ctx = getContext("2d")
          ctx.reset()
          var c = width / 2, r = width / 2 - 1.5
          ctx.lineWidth = 1.6
          if (root.mode === "error") {
            ctx.fillStyle = Color.urgent
            ctx.beginPath()
            ctx.moveTo(c, 1); ctx.lineTo(width - 1, height - 1); ctx.lineTo(1, height - 1)
            ctx.closePath(); ctx.fill()
          } else if (root.mode === "synced") {
            ctx.strokeStyle = root.ink
            ctx.beginPath()
            ctx.moveTo(1.5, c); ctx.lineTo(c - 1, height - 2.5); ctx.lineTo(width - 1.5, 2.5)
            ctx.stroke()
          } else if (root.mode === "measuring") {
            ctx.fillStyle = Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.25)
            ctx.beginPath(); ctx.arc(c, c, r * 0.55 * halo, 0, Math.PI * 2); ctx.fill()
            ctx.fillStyle = Color.accent
            ctx.beginPath(); ctx.arc(c, c, r * 0.5, 0, Math.PI * 2); ctx.fill()
          } else {
            ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.35)
            ctx.beginPath(); ctx.arc(c, c, r, 0, Math.PI * 2); ctx.stroke()
            ctx.strokeStyle = root.mode === "syncing" ? Color.accent : root.ink
            ctx.beginPath(); ctx.arc(c, c, r, turn, turn + Math.PI / 2); ctx.stroke()
          }
        }
        NumberAnimation on turn {
          running: root.visible && (root.mode === "waiting" || root.mode === "syncing")
          from: 0; to: Math.PI * 2
          duration: root.mode === "syncing" ? 900 : 1600
          loops: Animation.Infinite
        }
        SequentialAnimation on halo {
          running: root.visible && root.mode === "measuring"
          loops: Animation.Infinite
          NumberAnimation { from: 1.0; to: 1.6; duration: 600 }
          NumberAnimation { from: 1.6; to: 1.0; duration: 600 }
        }
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: root.label
      color: root.mode === "error" ? Color.urgent : root.ink
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }
}
