import QtQuick
import qs.Commons

// Primary buttons fill with the accent; their label takes whichever of the surface or the ink
// contrasts more with the accent (labelColor). Secondary buttons are outlined.
Rectangle {
  id: root
  property string text: ""
  property bool primary: false
  property color ink: Color.foreground
  property color labelColor: ink
  property string fontFamily: Style.font.family
  signal clicked()

  implicitWidth: label.implicitWidth + Style.space(28)
  implicitHeight: Style.space(32)
  radius: Style.cornerRadius
  color: primary ? (hit.containsMouse ? Qt.lighter(Color.accent, 1.08) : Color.accent) : (hit.containsMouse ? Qt.rgba(ink.r, ink.g, ink.b, 0.08) : "transparent")
  border.width: primary ? 0 : 1
  border.color: Qt.rgba(ink.r, ink.g, ink.b, 0.3)

  Text {
    id: label
    anchors.centerIn: parent
    text: root.text
    color: root.primary ? root.labelColor : root.ink
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    font.weight: root.primary ? Font.DemiBold : Font.Normal
  }

  MouseArea {
    id: hit
    anchors.fill: parent
    hoverEnabled: true
    onClicked: root.clicked()
  }
}
