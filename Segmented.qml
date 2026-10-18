import QtQuick
import qs.Commons

// A small segmented control: one option highlighted, chosen(index) on tap.
Rectangle {
  id: root
  property var options: []
  property int current: 0
  property color ink: Color.foreground
  property string fontFamily: Style.font.family
  property bool compact: false
  signal chosen(int index)

  implicitHeight: compact ? Style.space(24) : Style.space(30)
  radius: Style.cornerRadius
  color: Qt.rgba(ink.r, ink.g, ink.b, 0.05)

  Row {
    anchors.fill: parent
    anchors.margins: Style.space(2)
    Repeater {
      model: root.options
      delegate: Rectangle {
        required property string modelData
        required property int index
        width: parent.width / Math.max(1, root.options.length)
        height: parent.height
        radius: Style.cornerRadius
        color: index === root.current ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18) : "transparent"
        border.width: index === root.current ? 1 : 0
        border.color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.7)
        Text {
          anchors.centerIn: parent
          text: parent.modelData
          color: root.ink
          font.family: root.fontFamily
          font.pixelSize: root.compact ? Style.font.caption : Style.font.bodySmall
          font.weight: parent.index === root.current ? Font.DemiBold : Font.Normal
        }
        MouseArea {
          anchors.fill: parent
          onClicked: root.chosen(parent.index)
        }
      }
    }
  }
}
