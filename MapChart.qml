import QtQuick
import qs.Commons

// The AHA (or ESC) table as a systolic-by-diastolic map: nested L regions, the latest reading
// ringed with crosshairs, the previous ten hollow. Positions arrive as lane fractions from view.py.
Item {
  id: root
  property var model: ({})
  property color ink: Color.foreground
  property color ink2: Color.muted
  property color canvasColor: Color.background
  property string fontFamily: Style.font.family
  property int labelSize: Style.font.caption

  readonly property real gutterLeft: Style.space(30)
  readonly property real gutterBottom: Style.space(16)
  readonly property real plotW: width - gutterLeft
  readonly property real plotH: height - gutterBottom

  onModelChanged: chart.requestPaint()
  onWidthChanged: chart.requestPaint()
  onHeightChanged: chart.requestPaint()
  onInkChanged: chart.requestPaint()

  Canvas {
    id: chart
    anchors.fill: parent
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var regions = root.model.regions || []
      var top = Math.max(1, regions.length - 1)
      var x0 = root.gutterLeft
      var y0 = 0
      var w = root.plotW
      var h = root.plotH
      ctx.lineWidth = 1
      for (var i = 0; i < regions.length; i++) {
        var region = regions[i]
        var rank = regions.length - 1 - i
        var rw = region.x * w
        var rh = region.y * h
        ctx.fillStyle = root.canvasColor
        ctx.fillRect(x0, y0 + h - rh, rw, rh)
        var alpha = 0.20 * Math.pow(rank / top, 1.15)
        ctx.fillStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, alpha)
        ctx.fillRect(x0, y0 + h - rh, rw, rh)
        if (i === 0) {
          ctx.save()
          ctx.beginPath()
          ctx.rect(x0, y0, w, h)
          ctx.clip()
          ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.10)
          for (var d = -h; d < w; d += 6) {
            ctx.beginPath()
            ctx.moveTo(x0 + d, y0 + h)
            ctx.lineTo(x0 + d + h, y0)
            ctx.stroke()
          }
          ctx.restore()
        }
        ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.28)
        ctx.strokeRect(x0 + 0.5, y0 + h - rh + 0.5, rw - 1, rh - 1)
      }
      var points = root.model.points || []
      var latest = null
      for (var p = 0; p < points.length; p++) {
        var point = points[p]
        var px = x0 + point.x * w
        var py = y0 + h - point.y * h
        if (point.latest) { latest = { x: px, y: py }; continue }
        ctx.beginPath()
        ctx.arc(px, py, 2.6, 0, Math.PI * 2)
        ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.75)
        ctx.lineWidth = 1.2
        ctx.stroke()
      }
      if (latest) {
        ctx.setLineDash([2, 3])
        ctx.strokeStyle = Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.8)
        ctx.lineWidth = 1
        ctx.beginPath()
        ctx.moveTo(x0, latest.y)
        ctx.lineTo(latest.x, latest.y)
        ctx.moveTo(latest.x, latest.y)
        ctx.lineTo(latest.x, y0 + h)
        ctx.stroke()
        ctx.setLineDash([])
        ctx.beginPath()
        ctx.arc(latest.x, latest.y, 6.5, 0, Math.PI * 2)
        ctx.strokeStyle = Color.accent
        ctx.lineWidth = 1.5
        ctx.stroke()
        ctx.beginPath()
        ctx.arc(latest.x, latest.y, 3.6, 0, Math.PI * 2)
        ctx.fillStyle = Color.accent
        ctx.fill()
      }
    }
  }

  Repeater {
    model: root.model.regions || []
    delegate: Text {
      required property var modelData
      required property int index
      readonly property var inner: index + 1 < (root.model.regions || []).length ? root.model.regions[index + 1] : null
      readonly property real bandTop: root.plotH - modelData.y * root.plotH
      readonly property real bandBottom: inner ? root.plotH - inner.y * root.plotH : root.plotH
      // Right-aligned in the band's own strip, away from the readings, which sit to the left.
      visible: bandBottom - bandTop >= font.pixelSize + 2
      x: root.gutterLeft + modelData.x * root.plotW - width - Style.space(4)
      y: bandTop + Style.space(2)
      text: modelData.label
      color: modelData.key === root.model.current ? root.ink : root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
      font.bold: modelData.key === root.model.current
    }
  }

  Repeater {
    model: root.model.systolic || []
    delegate: Text {
      required property var modelData
      x: root.gutterLeft - width - Style.space(4)
      y: root.plotH - modelData.y * root.plotH - height / 2
      text: modelData.value
      color: root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
    }
  }

  Repeater {
    model: root.model.diastolic || []
    delegate: Text {
      required property var modelData
      x: root.gutterLeft + modelData.x * root.plotW - width / 2
      y: root.plotH + Style.space(2)
      text: modelData.value
      color: root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
    }
  }
}
