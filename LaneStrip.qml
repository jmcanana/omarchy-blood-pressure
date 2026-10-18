import QtQuick
import qs.Commons

// One trend strip: equal-height band lanes with names in a left gutter, day slots on a shared
// time axis, breaks where more than a day passed, readings joined only within a day.
// With no lanes (under 13) it draws a plain field with the lowest and highest values only.
Item {
  id: root
  property var strip: ({})
  property var days: []
  property var breaks: []
  property color ink: Color.foreground
  property color ink2: Color.muted
  property color canvasColor: Color.background
  property string fontFamily: Style.font.family
  property int labelSize: Style.font.caption
  property int laneHeight: Style.space(15)
  property string unit: ""
  property int selected: -1
  signal picked(int index)

  readonly property var lanes: strip.lanes || []
  readonly property bool plain: lanes.length === 0
  property real gutterLeft: Style.space(62)
  readonly property real gutterRight: Style.space(30)
  readonly property real plotX: gutterLeft
  readonly property real plotW: width - gutterLeft - gutterRight
  readonly property real plotH: plain ? Style.space(48) : lanes.length * laneHeight

  implicitHeight: plotH

  onStripChanged: canvas.requestPaint()
  onDaysChanged: canvas.requestPaint()
  onWidthChanged: canvas.requestPaint()
  onSelectedChanged: canvas.requestPaint()
  onInkChanged: canvas.requestPaint()

  function yFor(fraction) { return root.plotH - fraction * root.plotH }

  Canvas {
    id: canvas
    anchors.fill: parent
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var x0 = root.plotX, w = root.plotW, h = root.plotH
      var days = root.days || []
      var lanes = root.lanes
      var top = Math.max(1, lanes.length - 1)
      for (var d = 0; d < days.length; d++) {
        var dx = x0 + days[d].x0 * w + 1
        var dw = (days[d].x1 - days[d].x0) * w - 2
        if (root.plain) {
          ctx.fillStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.05)
          ctx.fillRect(dx, 0, dw, h)
          continue
        }
        for (var l = 0; l < lanes.length; l++) {
          var ly = h - (l + 1) * root.laneHeight
          var alpha = lanes[l].key === "usual" ? 0.07 : 0.20 * Math.pow(l / top, 1.15) + 0.03
          ctx.fillStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, alpha)
          ctx.fillRect(dx, ly + 0.5, dw, root.laneHeight - 1)
          if (lanes[l].urgent && l === lanes.length - 1) {
            ctx.save()
            ctx.beginPath()
            ctx.rect(dx, ly, dw, root.laneHeight)
            ctx.clip()
            ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.14)
            ctx.lineWidth = 1
            for (var k = -root.laneHeight; k < dw; k += 6) {
              ctx.beginPath()
              ctx.moveTo(dx + k, ly + root.laneHeight)
              ctx.lineTo(dx + k + root.laneHeight, ly)
              ctx.stroke()
            }
            ctx.restore()
          }
        }
      }
      var points = root.strip.points || []
      ctx.lineWidth = 1
      ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.55)
      for (var p = 1; p < points.length; p++) {
        if (points[p].day !== points[p - 1].day) continue
        ctx.beginPath()
        ctx.moveTo(x0 + points[p - 1].x * w, root.yFor(points[p - 1].y))
        ctx.lineTo(x0 + points[p].x * w, root.yFor(points[p].y))
        ctx.stroke()
      }
      for (var q = 0; q < points.length; q++) {
        var px = x0 + points[q].x * w
        var py = root.yFor(points[q].y)
        var last = q === points.length - 1
        ctx.beginPath()
        ctx.arc(px, py, last ? 4.2 : 2.4, 0, Math.PI * 2)
        ctx.fillStyle = last ? Color.accent : root.ink
        ctx.fill()
        if (q === root.selected) {
          ctx.beginPath()
          ctx.arc(px, py, 6.5, 0, Math.PI * 2)
          ctx.strokeStyle = Color.urgent
          ctx.lineWidth = 1.5
          ctx.stroke()
          ctx.lineWidth = 1
          ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.55)
        }
      }
    }
  }

  Repeater {
    model: root.plain ? [] : root.lanes
    delegate: Text {
      required property var modelData
      required property int index
      x: 0
      width: root.gutterLeft - Style.space(6)
      y: root.plotH - (index + 1) * root.laneHeight + (root.laneHeight - height) / 2
      horizontalAlignment: Text.AlignLeft
      text: modelData.label
      elide: Text.ElideRight
      color: modelData.key === root.strip.latest_lane ? root.ink : root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
      font.bold: modelData.key === root.strip.latest_lane
    }
  }

  Repeater {
    model: root.plain ? [] : root.lanes.slice(1)
    delegate: Text {
      required property var modelData
      required property int index
      x: root.plotX + root.plotW + Style.space(4)
      y: root.plotH - (index + 1) * root.laneHeight - height / 2
      text: modelData.low
      color: root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
    }
  }

  Column {
    visible: root.plain
    x: root.plotX + root.plotW + Style.space(4)
    height: root.plotH
    Text {
      text: root.strip.range ? root.strip.range[1] : ""
      color: root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
    }
    Item { width: 1; height: root.plotH - 2 * Style.font.caption - Style.space(6) }
    Text {
      text: root.strip.range ? root.strip.range[0] : ""
      color: root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
    }
  }

  MouseArea {
    x: root.plotX
    width: root.plotW
    height: root.plotH
    onClicked: function(mouse) {
      var points = root.strip.points || []
      var best = -1, bestDistance = 1e9
      for (var i = 0; i < points.length; i++) {
        var distance = Math.abs(points[i].x * root.plotW - mouse.x)
        if (distance < bestDistance) { bestDistance = distance; best = i }
      }
      root.picked(bestDistance < Style.space(10) ? best : -1)
    }
  }
}
