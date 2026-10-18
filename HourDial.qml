import QtQuick
import qs.Commons

// The 24-hour dial, 0 at the top. Radius is mmHg. Each hour's spoke spans the lowest to highest
// systolic (bold) and diastolic (light) readings taken in that hour; a single reading is a short
// tick. Rings mark 80, 120, 140 and 180 mmHg. An hour with a stage 2 or higher reading is urgent.
Item {
  id: root
  property var hours: []
  property color ink: Color.foreground
  property color ink2: Color.muted
  property string fontFamily: Style.font.family
  property int labelSize: Style.font.caption

  readonly property real low: 40
  readonly property real high: 200
  readonly property real innerR: Math.min(width, height) * 0.14
  readonly property real outerR: Math.min(width, height) * 0.44
  readonly property var rings: [80, 120, 140, 180]

  function radius(value) {
    var clamped = Math.max(root.low, Math.min(root.high, value))
    return root.innerR + (clamped - root.low) / (root.high - root.low) * (root.outerR - root.innerR)
  }

  onHoursChanged: dial.requestPaint()
  onWidthChanged: dial.requestPaint()
  onInkChanged: dial.requestPaint()

  Canvas {
    id: dial
    anchors.fill: parent
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var cx = width / 2, cy = height / 2
      ctx.lineWidth = 1
      for (var r = 0; r < root.rings.length; r++) {
        ctx.strokeStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, root.rings[r] >= 140 ? 0.22 : 0.12)
        ctx.setLineDash(root.rings[r] >= 140 ? [] : [2, 3])
        ctx.beginPath()
        ctx.arc(cx, cy, root.radius(root.rings[r]), 0, Math.PI * 2)
        ctx.stroke()
      }
      ctx.setLineDash([])
      var step = Math.PI * 2 / 24
      for (var h = 0; h < 24; h++) {
        var item = root.hours[h]
        var angle = -Math.PI / 2 + (h + 0.5) * step
        var cos = Math.cos(angle), sin = Math.sin(angle)
        if (!item || !item.n) {
          ctx.fillStyle = Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.10)
          ctx.beginPath()
          ctx.arc(cx + cos * root.innerR * 0.8, cy + sin * root.innerR * 0.8, 1.2, 0, Math.PI * 2)
          ctx.fill()
          continue
        }
        var colour = item.urgent ? Color.urgent : root.ink
        root.spoke(ctx, cx, cy, cos, sin, item.dia_min, item.dia_max, 3, Qt.rgba(colour.r, colour.g, colour.b, 0.45))
        root.spoke(ctx, cx, cy, cos, sin, item.sys_min, item.sys_max, 5, Qt.rgba(colour.r, colour.g, colour.b, 0.95))
      }
    }
  }

  function spoke(ctx, cx, cy, cos, sin, from, to, thickness, colour) {
    var r0 = root.radius(from), r1 = Math.max(root.radius(to), r0 + 3)
    ctx.strokeStyle = colour
    ctx.lineWidth = thickness
    ctx.lineCap = "round"
    ctx.beginPath()
    ctx.moveTo(cx + cos * r0, cy + sin * r0)
    ctx.lineTo(cx + cos * r1, cy + sin * r1)
    ctx.stroke()
  }

  Repeater {
    model: [{ t: "0", a: 0 }, { t: "6", a: 6 }, { t: "12", a: 12 }, { t: "18", a: 18 }]
    delegate: Text {
      required property var modelData
      readonly property real angle: -Math.PI / 2 + modelData.a * Math.PI / 12
      readonly property real reach: root.outerR + Style.space(10)
      x: root.width / 2 + Math.cos(angle) * reach - width / 2
      y: root.height / 2 + Math.sin(angle) * reach - height / 2
      text: modelData.t
      color: root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize
    }
  }

  // Ring values run out along the 3 a.m. spoke, where readings are rarest.
  Repeater {
    model: root.rings
    delegate: Text {
      required property real modelData
      readonly property real angle: -Math.PI / 4
      x: root.width / 2 + Math.cos(angle) * root.radius(modelData) + 1
      y: root.height / 2 + Math.sin(angle) * root.radius(modelData) - height + 2
      text: modelData
      color: root.ink2
      font.family: root.fontFamily
      font.pixelSize: root.labelSize - 1
    }
  }
}
