import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Direction B, "Map": the latest reading on the systolic-by-diastolic map, then systolic,
// diastolic and pulse trends on equal-height band lanes, the 24-hour dial and the device strip.
// Every number arrives from the sidecar's view model (pressure/view.py); this file only draws.
Panel {
  id: root
  moduleName: "jmcanana.pressure"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var summary: ({})
  property string linkState: "idle"
  property int commandId: 1
  property int selected: -1
  property bool editing: false
  property var editPatient: null
  property string draftAlias: ""
  property bool draftUnder13: false
  property int draftCuffUser: 1

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateDir: home + "/.local/state/jmcanana.pressure"
  readonly property string pluginDir: {
    var folder = decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, ""))
    return folder.endsWith("/") ? folder : folder + "/"
  }
  readonly property color ink: Color.popups.text
  readonly property color surface: Color.popups.background
  readonly property color ink2: Qt.rgba(ink.r, ink.g, ink.b, inkAlpha(ink, surface, 4.5))
  readonly property string family: Style.font.family
  readonly property var hero: summary.hero || ({ present: false })
  readonly property var trend: summary.trend || ({ days: [], breaks: [], strips: {}, readings: [] })
  readonly property var strips: trend.strips || ({})
  readonly property bool child: summary.child === true
  readonly property var monitor: (summary.monitors || []).length ? summary.monitors[0] : null
  readonly property var patients: summary.patients || []
  readonly property int heroSize: Math.round(Style.font.displayLarge * 1.85)

  // Secondary text: the lowest foreground alpha that still reaches 4.5:1 on this theme's surface.
  function luminance(c) {
    function channel(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
    return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b)
  }
  function contrast(a, b) {
    var la = luminance(a), lb = luminance(b)
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
  }
  function inkAlpha(fg, bg, target) {
    for (var i = 30; i <= 100; i++) {
      var a = i / 100
      var mixed = Qt.rgba(bg.r + a * (fg.r - bg.r), bg.g + a * (fg.g - bg.g), bg.b + a * (fg.b - bg.b), 1)
      if (contrast(mixed, bg) >= target) return a
    }
    return 1
  }
  function onAccent() {
    return contrast(root.surface, Color.accent) >= contrast(root.ink, Color.accent) ? root.surface : root.ink
  }

  function open() {
    root.editing = false
    root.selected = -1
    root.controller.show()
    root.askSummary()
  }
  function close() { root.controller.hide() }
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  // The sidecar reads one command file at a time, so commands queue here and go out one by one,
  // each after the previous response. Anything that changes data is followed by a fresh summary.
  property var queue: []
  property string inflight: ""
  property bool dirty: false
  function command(op, extra) {
    var body = extra || {}
    body.op = op
    root.queue = root.queue.concat([body])
    if (op !== "summary") root.dirty = true
    root.dispatch()
  }
  function dispatch() {
    if (root.inflight !== "" || root.queue.length === 0) return
    var body = root.queue[0]
    root.queue = root.queue.slice(1)
    body.id = String(root.commandId)
    root.commandId += 1
    root.inflight = body.id
    flushGuard.restart()
    enqueue.running = false
    enqueue.command = ["python3", root.pluginDir + "pressure/enqueue.py", root.stateDir + "/command.json", JSON.stringify(body)]
    enqueue.running = true
  }
  function askSummary() {
    if (root.queue.some(function(item) { return item.op === "summary" })) return
    root.command("summary", { patient: root.summary.patient_id || "" })
  }
  function applyResponse(raw) {
    var doc
    try { doc = JSON.parse(raw || "{}") } catch (error) { return }
    if (!doc || String(doc.id) !== root.inflight) return
    root.inflight = ""
    if (doc.hero !== undefined) root.summary = doc
    if (root.queue.length === 0 && root.dirty) {
      root.dirty = false
      root.askSummary()
    }
    root.dispatch()
  }

  // The lane-name gutter fits the longest band name in the active table ("Hypertension" under ESC).
  readonly property real laneGutter: {
    var widest = 0
    var keys = ["systolic", "diastolic", "pulse"]
    for (var k = 0; k < keys.length; k++) {
      var lanes = (root.strips[keys[k]] || {}).lanes || []
      for (var i = 0; i < lanes.length; i++) {
        laneMetrics.text = lanes[i].label
        widest = Math.max(widest, laneMetrics.advanceWidth)
      }
    }
    return Math.max(Style.space(62), Math.ceil(widest) + Style.space(10))
  }
  TextMetrics {
    id: laneMetrics
    font.family: root.family
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  property bool capturing: false
  property bool copied: false
  readonly property string sharePath: root.stateDir + "/pressure-share.png"
  function copyImage() {
    root.capturing = true
    Qt.callLater(function() {
      shareFrame.grabToImage(function(result) {
        result.saveToFile(root.sharePath)
        root.capturing = false
        clipboard.running = false
        clipboard.command = ["sh", "-c", "wl-copy --type image/png < \"$1\"", "sh", root.sharePath]
        clipboard.running = true
        root.copied = true
        copiedReset.restart()
      })
    })
  }
  // Day numbers where their slot leaves room, the month where it changes; never overlapping.
  function axisLabels(days, width) {
    var labels = [], lastDay = -1e9, lastMonth = -1e9, gap = Style.space(18)
    for (var i = 0; i < days.length; i++) {
      var centre = (days[i].x0 + days[i].x1) / 2 * width
      if (centre - lastDay >= gap) { labels.push({ x: centre, text: String(days[i].day), row: 0 }); lastDay = centre }
      if (days[i].month && centre - lastMonth >= gap * 1.6) { labels.push({ x: centre, text: days[i].month, row: 1 }); lastMonth = centre }
    }
    return labels
  }

  function noticeTitle(code) {
    if (code === "irregular-latest") return "Irregular pulse flagged on the latest reading"
    if (code === "irregular-repeat") return "Irregular pulse flagged on two or more of the last 14"
    if (code === "often-high") return "More than half of the last 14 readings are stage 2 or above"
    if (code === "morning-evening") return "Morning and evening medians differ by 10 mmHg or more"
    return ""
  }
  function noticeCaption(code) {
    if (code.indexOf("irregular") === 0) return "The cuff's own flag. Show the readings to a clinician."
    if (code === "often-high") return "From the AHA table; not a diagnosis."
    return "Each side has at least four readings."
  }
  function selectedLine() {
    var list = root.trend.readings || []
    if (root.selected < 0 || root.selected >= list.length) return ""
    var r = list[root.selected]
    var when = (r.taken_at || "").replace("T", " ").slice(0, 16)
    return when + " · " + r.systolic + "/" + r.diastolic + " mmHg · pulse " + r.pulse + (r.irregular ? " · irregular" : "")
  }
  function openEditor(person) {
    root.editPatient = person
    root.draftAlias = person ? person.alias : ""
    root.draftUnder13 = person ? person.under_13 === true : false
    root.draftCuffUser = person && person.cuff_users && person.cuff_users.length ? person.cuff_users[0] : 1
    root.editing = true
  }
  function saveEditor() {
    var alias = root.draftAlias.trim()
    if (!alias) return
    if (root.editPatient) {
      var id = root.editPatient.id
      if (alias !== root.editPatient.alias) root.command("rename_patient", { patient: id, alias: alias })
      root.command("set_under_13", { patient: id, under_13: root.draftUnder13 })
      root.command("bind", { patient: id, user_byte: root.draftCuffUser })
    } else {
      root.command("add_patient", { alias: alias })
    }
    root.editing = false
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(Style.space(900))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: header
        x: Style.space(20)
        width: parent.width - Style.space(40)
        topPadding: Style.space(18)
        spacing: Style.space(14)

        Item {
          width: parent.width
          height: pill.height
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Pressure"
            color: root.ink
            font.family: root.family
            font.pixelSize: Style.font.heading
            font.weight: Font.DemiBold
          }
          SyncPill {
            id: pill
            anchors.right: parent.right
            linkState: root.linkState
            lastSync: root.summary.last_sync || null
            ink: root.ink
            ink2: root.ink2
            fontFamily: root.family
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(8)
          readonly property real segmentWidth: (width - plusButton.width - spacing) / Math.max(1, root.patients.length)

          Rectangle {
            width: parent.width - plusButton.width - parent.spacing
            height: Style.space(34)
            radius: Style.cornerRadius
            color: Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.05)
            visible: root.patients.length > 0
            Row {
              anchors.fill: parent
              anchors.margins: Style.space(3)
              Repeater {
                model: root.patients
                delegate: Rectangle {
                  required property var modelData
                  readonly property bool active: modelData.id === root.summary.patient_id
                  width: (parent.width) / Math.max(1, root.patients.length)
                  height: parent.height
                  radius: Style.cornerRadius
                  color: active ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.16) : "transparent"
                  border.width: active ? 1 : 0
                  border.color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.7)
                  Text {
                    anchors.left: parent.left
                    anchors.leftMargin: Style.space(12)
                    anchors.verticalCenter: parent.verticalCenter
                    text: parent.modelData.alias
                    textFormat: Text.PlainText
                    color: root.ink
                    font.family: root.family
                    font.pixelSize: Style.font.body
                    font.weight: parent.active ? Font.DemiBold : Font.Normal
                  }
                  Text {
                    anchors.right: parent.right
                    anchors.rightMargin: Style.space(10)
                    anchors.verticalCenter: parent.verticalCenter
                    text: (parent.modelData.cuff_users || []).length ? "cuff " + parent.modelData.cuff_users.join("/") : ""
                    color: root.ink2
                    font.family: root.family
                    font.pixelSize: Style.font.caption
                  }
                  MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    onClicked: function(mouse) {
                      if (mouse.button === Qt.RightButton || parent.active) { root.openEditor(parent.modelData); return }
                      root.summary = Object.assign({}, root.summary, { patient_id: parent.modelData.id })
                      root.command("set_active", { patient: parent.modelData.id })
                    }
                  }
                }
              }
            }
          }

          Text {
            visible: root.patients.length === 0
            width: parent.width - plusButton.width - parent.spacing
            height: Style.space(34)
            verticalAlignment: Text.AlignVCenter
            text: "Add a patient to start."
            color: root.ink2
            font.family: root.family
            font.pixelSize: Style.font.body
          }

          Rectangle {
            id: plusButton
            width: Style.space(34)
            height: Style.space(34)
            radius: Style.cornerRadius
            color: plusHit.containsMouse ? Style.hoverFill : Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.05)
            border.width: 1
            border.color: Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.16)
            Text {
              anchors.centerIn: parent
              text: "+"
              color: root.ink
              font.family: root.family
              font.pixelSize: Style.font.heading
            }
            MouseArea {
              id: plusHit
              anchors.fill: parent
              hoverEnabled: true
              onClicked: root.openEditor(null)
            }
          }
        }
      }

      Flickable {
        id: flick
        anchors.top: header.bottom
        anchors.topMargin: Style.space(10)
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        contentWidth: width
        contentHeight: shareFrame.height
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        // Everything below the header, on an opaque surface, so "Copy image" grabs a clean picture.
        Rectangle {
          id: shareFrame
          width: flick.width
          height: body.implicitHeight + Style.space(20) + (root.capturing ? Style.space(24) : 0)
          color: root.surface

        Column {
          id: body
          x: Style.space(20)
          y: root.capturing ? Style.space(20) : 0
          width: flick.width - Style.space(40)
          spacing: Style.space(16)

          Text {
            visible: root.capturing
            text: "Pressure · " + (root.summary.patient || "") + " · " + Qt.formatDateTime(new Date(), "MMM d, yyyy")
            textFormat: Text.PlainText
            color: root.ink
            font.family: root.family
            font.pixelSize: Style.font.heading
            font.weight: Font.DemiBold
          }

          // Patient editor
          Rectangle {
            visible: root.editing
            width: parent.width
            height: editor.implicitHeight + Style.space(28)
            radius: Style.cornerRadius
            color: Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.04)
            border.width: 1
            border.color: Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.14)
            Column {
              id: editor
              x: Style.space(16)
              y: Style.space(14)
              width: parent.width - Style.space(32)
              spacing: Style.space(8)
              Text { text: root.editPatient ? "Patient" : "New patient"; color: root.ink; font.family: root.family; font.pixelSize: Style.font.title; font.weight: Font.DemiBold }
              Text { text: "Name"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
              Rectangle {
                width: parent.width
                height: Style.space(32)
                radius: Style.cornerRadius
                color: "transparent"
                border.width: 1
                border.color: aliasInput.activeFocus ? Color.accent : Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.3)
                TextInput {
                  id: aliasInput
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(10)
                  anchors.rightMargin: Style.space(10)
                  verticalAlignment: TextInput.AlignVCenter
                  text: root.draftAlias
                  onTextEdited: root.draftAlias = text
                  maximumLength: 24
                  color: root.ink
                  font.family: root.family
                  font.pixelSize: Style.font.body
                  onAccepted: root.saveEditor()
                }
              }
              Text { text: "Age group"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
              Segmented {
                width: parent.width
                options: ["13 or older", "Under 13"]
                current: root.draftUnder13 ? 1 : 0
                ink: root.ink
                fontFamily: root.family
                onChosen: function(index) { root.draftUnder13 = index === 1 }
              }
              Text { width: parent.width; wrapMode: Text.WordWrap; text: "Bands apply from 13. No age or birth date is stored."; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
              Text { visible: root.editPatient !== null; text: "Cuff user"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
              Segmented {
                visible: root.editPatient !== null
                width: parent.width
                options: ["1", "2"]
                current: root.draftCuffUser === 2 ? 1 : 0
                ink: root.ink
                fontFamily: root.family
                onChosen: function(index) { root.draftCuffUser = index + 1 }
              }
              Row {
                spacing: Style.space(8)
                ActionButton { text: "Save"; primary: true; ink: root.ink; fontFamily: root.family; labelColor: root.onAccent(); onClicked: root.saveEditor() }
                ActionButton { text: "Cancel"; ink: root.ink; fontFamily: root.family; onClicked: root.editing = false }
              }
            }
          }

          // Trust card
          Rectangle {
            visible: !!root.summary.seen
            width: parent.width
            height: trustColumn.implicitHeight + Style.space(28)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.08)
            border.width: 1
            border.color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.65)
            Column {
              id: trustColumn
              x: Style.space(16)
              y: Style.space(14)
              width: parent.width - Style.space(32)
              spacing: Style.space(8)
              readonly property bool generic: root.summary.seen ? root.summary.seen.beacon === "generic" : false
              Text { text: "New monitor nearby"; color: root.ink; font.family: root.family; font.pixelSize: Style.font.title; font.weight: Font.DemiBold }
              Text {
                text: root.summary.seen ? ((root.summary.seen.name || "Monitor") + " · ends " + root.summary.seen.tail) : ""
                textFormat: Text.PlainText
                color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption
              }
              Text {
                width: parent.width
                wrapMode: Text.WordWrap
                text: trustColumn.generic
                  ? "An unknown blood-pressure advert is on the air. Trust it only if that monitor is yours. One tap does not treat it as this cuff."
                  : "Trust it only if this monitor is yours. After that, every measurement syncs by itself while its Bluetooth light flashes."
                color: root.ink; font.family: root.family; font.pixelSize: Style.font.body
              }
              Row {
                spacing: Style.space(8)
                ActionButton {
                  text: trustColumn.generic ? "Trust this unknown advert" : "Trust this monitor"
                  primary: true; ink: root.ink; fontFamily: root.family; labelColor: root.onAccent()
                  onClicked: root.command("trust", { token: root.summary.seen.token, confirm_generic: trustColumn.generic })
                }
                ActionButton {
                  text: "Not mine"; ink: root.ink; fontFamily: root.family
                  onClicked: root.command("dismiss_seen", { token: root.summary.seen.token })
                }
              }
            }
          }

          // Hero
          Column {
            width: parent.width
            spacing: Style.space(10)
            visible: root.hero.present === true

            Text {
              text: root.hero.kicker || ""
              color: root.ink2
              font.family: root.family
              font.pixelSize: Style.font.caption
              font.capitalization: Font.AllUppercase
              font.letterSpacing: 1
            }

            Row {
              width: parent.width
              spacing: Style.space(14)

              Column {
                width: root.child ? parent.width : Style.space(150)
                spacing: Style.space(2)
                Row {
                  spacing: Style.space(6)
                  Text { text: root.hero.systolic === undefined ? "" : root.hero.systolic; color: root.hero.urgent ? Color.urgent : root.ink; font.family: root.family; font.pixelSize: root.heroSize; font.weight: Font.Light; font.features: { "tnum": 1 } }
                  Text { anchors.bottom: parent.bottom; anchors.bottomMargin: Style.space(8); text: "SYS"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
                }
                Row {
                  spacing: Style.space(6)
                  Text { text: root.hero.diastolic === undefined ? "" : root.hero.diastolic; color: root.hero.urgent ? Color.urgent : root.ink; font.family: root.family; font.pixelSize: root.heroSize; font.weight: Font.Light; font.features: { "tnum": 1 } }
                  Text { anchors.bottom: parent.bottom; anchors.bottomMargin: Style.space(8); text: "DIA"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
                }
                Text { text: "mmHg"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
                Row {
                  topPadding: Style.space(8)
                  spacing: Style.space(6)
                  Text { text: "♥"; color: root.hero.irregular ? Color.urgent : root.ink2; font.family: root.family; font.pixelSize: Style.font.title; anchors.verticalCenter: parent.verticalCenter }
                  Text { text: root.hero.pulse === undefined || root.hero.pulse === null ? "–" : root.hero.pulse; color: root.ink; font.family: root.family; font.pixelSize: Style.font.display; font.weight: Font.Light; font.features: { "tnum": 1 } }
                  Text { anchors.verticalCenter: parent.verticalCenter; text: root.hero.irregular ? "bpm · irregular" : "bpm · cuff pulse"; color: root.hero.irregular ? Color.urgent : root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
                }
              }

              Column {
                visible: !root.child
                width: parent.width - Style.space(150) - parent.spacing
                spacing: Style.space(4)
                Text { anchors.right: parent.right; text: "sys ↑ · dia →"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
                MapChart {
                  width: parent.width
                  height: Style.space(170)
                  model: root.summary.map || ({})
                  ink: root.ink
                  ink2: root.ink2
                  canvasColor: root.surface
                  fontFamily: root.family
                }
              }
            }

            Item {
              width: parent.width
              height: categoryText.height
              visible: !root.child
              Row {
                spacing: Style.space(8)
                Text {
                  id: categoryText
                  text: (root.hero.urgent ? "▲ " : "") + (root.hero.category_label || "")
                  color: root.hero.urgent ? Color.urgent : root.ink
                  font.family: root.family
                  font.pixelSize: Style.font.heading + 4
                  font.weight: root.hero.urgent ? Font.Bold : Font.DemiBold
                }
              }
              Segmented {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(96)
                options: ["AHA", "ESC"]
                current: root.summary.guideline === "esc" ? 1 : 0
                ink: root.ink
                fontFamily: root.family
                compact: true
                onChosen: function(index) { root.command("guideline", { guideline: index === 1 ? "esc" : "aha" }) }
              }
            }
            Text {
              visible: !root.child && !!root.hero.explain
              width: parent.width
              wrapMode: Text.WordWrap
              text: root.hero.explain || ""
              color: root.ink2
              font.family: root.family
              font.pixelSize: Style.font.bodySmall
            }

            Rectangle {
              visible: root.child
              width: parent.width
              height: childColumn.implicitHeight + Style.space(24)
              radius: Style.cornerRadius
              color: Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.05)
              Column {
                id: childColumn
                x: Style.space(14)
                y: Style.space(12)
                width: parent.width - Style.space(28)
                spacing: Style.space(4)
                Text { text: "Not classified under 13"; color: root.ink; font.family: root.family; font.pixelSize: Style.font.title; font.weight: Font.DemiBold }
                Text { width: parent.width; wrapMode: Text.WordWrap; text: "Children's blood pressure is read against percentiles for age, sex and height; ask their doctor."; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
              }
            }
          }

          Text {
            visible: root.hero.present !== true && !root.summary.seen
            width: parent.width
            wrapMode: Text.WordWrap
            text: root.patients.length === 0
              ? "Add a patient, then take a measurement. When the cuff's Bluetooth light flashes, the reading arrives here."
              : "No readings yet. Take a measurement; when the cuff's Bluetooth light flashes, it syncs."
            color: root.ink2
            font.family: root.family
            font.pixelSize: Style.font.body
          }

          // Severe guidance
          Rectangle {
            visible: root.hero.crisis === true
            width: parent.width
            height: severeColumn.implicitHeight + Style.space(28)
            radius: Style.cornerRadius
            color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.08)
            border.width: 1
            border.color: Color.urgent
            Rectangle { width: Style.space(5); height: parent.height; radius: Style.cornerRadius; color: Color.urgent }
            Column {
              id: severeColumn
              x: Style.space(20)
              y: Style.space(14)
              width: parent.width - Style.space(34)
              spacing: Style.space(6)
              Text { text: "▲ Severe range"; color: Color.urgent; font.family: root.family; font.pixelSize: Style.font.title; font.weight: Font.Bold }
              Text { width: parent.width; wrapMode: Text.WordWrap; text: "If any of these are present, call emergency services now:"; color: root.ink; font.family: root.family; font.pixelSize: Style.font.body }
              Grid {
                columns: 2
                columnSpacing: Style.space(20)
                rowSpacing: Style.space(2)
                Repeater {
                  model: ["chest pain", "shortness of breath", "back pain", "numbness", "weakness", "change in vision", "difficulty speaking"]
                  delegate: Text { required property string modelData; text: "• " + modelData; color: root.ink; font.family: root.family; font.pixelSize: Style.font.bodySmall }
                }
              }
              Text { width: parent.width; wrapMode: Text.WordWrap; text: "No symptoms: wait five minutes and measure again. If it is still this high, contact a clinician. AHA table; not a diagnosis."; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
            }
          }

          // Notices
          Repeater {
            model: (root.summary.notices || []).filter(function(code) { return root.noticeTitle(code) !== "" })
            delegate: Rectangle {
              required property string modelData
              width: body.width
              height: noticeColumn.implicitHeight + Style.space(20)
              radius: Style.cornerRadius
              color: "transparent"
              border.width: 1.2
              border.color: modelData.indexOf("irregular") === 0 ? Color.urgent : Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.3)
              Column {
                id: noticeColumn
                x: Style.space(14)
                y: Style.space(10)
                width: parent.width - Style.space(28)
                spacing: Style.space(2)
                Text { width: parent.width; wrapMode: Text.WordWrap; text: root.noticeTitle(parent.parent.modelData); color: root.ink; font.family: root.family; font.pixelSize: Style.font.body; font.weight: Font.DemiBold }
                Text { width: parent.width; wrapMode: Text.WordWrap; text: root.noticeCaption(parent.parent.modelData); color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
              }
            }
          }

          // Trend
          Column {
            visible: (root.trend.readings || []).length > 0
            width: parent.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              height: trendTitle.height
              Text { id: trendTitle; text: "Trend"; color: root.ink; font.family: root.family; font.pixelSize: Style.font.title; font.weight: Font.DemiBold }
              Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: root.trend.span || ""; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
            }

            Repeater {
              model: [
                { key: "systolic", title: "SYSTOLIC · mmHg" },
                { key: "diastolic", title: "DIASTOLIC · mmHg" },
                { key: "pulse", title: "PULSE · bpm" }
              ]
              delegate: Column {
                required property var modelData
                width: body.width
                spacing: Style.space(4)
                Text {
                  text: parent.modelData.title + (root.child ? " · lowest and highest shown" : "")
                  color: root.ink2
                  font.family: root.family
                  font.pixelSize: Style.font.caption
                  font.letterSpacing: 1
                }
                LaneStrip {
                  width: parent.width
                  height: implicitHeight
                  strip: root.strips[parent.modelData.key] || ({})
                  days: root.trend.days || []
                  breaks: root.trend.breaks || []
                  ink: root.ink
                  ink2: root.ink2
                  canvasColor: root.surface
                  fontFamily: root.family
                  selected: root.selected
                  gutterLeft: root.laneGutter
                  onPicked: function(index) { root.selected = index }
                }
                Text {
                  width: parent.width
                  wrapMode: Text.WordWrap
                  text: (root.strips[parent.modelData.key] || {}).sentence || ""
                  color: root.ink2
                  font.family: root.family
                  font.pixelSize: Style.font.caption
                }
              }
            }

            // Shared day axis under the strips: day numbers where they fit, months where they change.
            Item {
              id: dayAxis
              width: parent.width
              height: Style.space(28)
              readonly property real plotX: root.laneGutter
              readonly property real plotW: width - root.laneGutter - Style.space(30)
              Repeater {
                model: root.axisLabels(root.trend.days || [], dayAxis.plotW)
                delegate: Text {
                  required property var modelData
                  x: dayAxis.plotX + modelData.x - width / 2
                  y: modelData.row * (Style.font.caption + Style.space(3))
                  text: modelData.text
                  color: root.ink2
                  font.family: root.family
                  font.pixelSize: Style.font.caption
                }
              }
            }
            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: root.selected >= 0 ? root.selectedLine() : "Each column is a day with readings; a gap between columns means days without any. Tap a reading for its details."
              color: root.selected >= 0 ? root.ink : root.ink2
              font.family: root.family
              font.pixelSize: Style.font.caption
            }
          }

          // Time of day
          Column {
            visible: (root.trend.readings || []).length > 0
            width: parent.width
            spacing: Style.space(8)
            Item {
              width: parent.width
              height: todTitle.height
              Text { id: todTitle; text: "Time of day"; color: root.ink; font.family: root.family; font.pixelSize: Style.font.title; font.weight: Font.DemiBold }
              Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: "cuff clock"; color: root.ink2; font.family: root.family; font.pixelSize: Style.font.caption }
            }
            Row {
              width: parent.width
              spacing: Style.space(12)
              HourDial {
                width: Style.space(200)
                height: Style.space(200)
                hours: root.summary.hours || []
                ink: root.ink
                ink2: root.ink2
                fontFamily: root.family
              }
              Column {
                width: parent.width - Style.space(200) - parent.spacing
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(6)
                readonly property var c: root.summary.contrast || ({})
                Text { text: "Morning and evening"; color: root.ink; font.family: root.family; font.pixelSize: Style.font.body; font.weight: Font.DemiBold }
                Text {
                  width: parent.width
                  wrapMode: Text.WordWrap
                  color: root.ink2
                  font.family: root.family
                  font.pixelSize: Style.font.caption
                  text: {
                    var c = parent.c
                    var counts = "morning " + (c.morning_n || 0) + ", evening " + (c.evening_n || 0)
                    if (root.summary.clock_skew_minutes !== null && root.summary.clock_skew_minutes !== undefined)
                      return "Not compared: the cuff clock was about " + root.summary.clock_skew_minutes + " minutes off at the last sync. Readings so far: " + counts + "."
                    if (!root.summary.clock_checked)
                      return "Not compared yet: the cuff clock has not been checked against this computer. Readings so far: " + counts + "."
                    if (!c.ready)
                      return "A comparison appears after four readings on each side. Readings so far: " + counts + "."
                    return (c.morning_higher ? "Morning runs higher" : (c.wider ? "The sides differ" : "Within 10 mmHg")) + " (" + counts + "). Morning is 4:00–9:59, evening 18:00–1:59."
                  }
                }
              }
            }
          }

          Text {
            visible: (root.trend.readings || []).length > 0
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Each spoke spans the lowest to highest reading taken in that hour of the day: systolic bold, diastolic light. Rings mark 80, 120, 140 and 180 mmHg; an hour with a stage 2 or higher reading is drawn in the urgent colour."
            color: root.ink2
            font.family: root.family
            font.pixelSize: Style.font.caption
          }

          // Device strip
          Column {
            width: parent.width
            spacing: Style.space(4)
            Rectangle { width: parent.width; height: 1; color: Qt.rgba(root.ink.r, root.ink.g, root.ink.b, 0.12) }
            Item {
              width: parent.width
              height: Style.space(22)
              Text {
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: root.monitor
                  ? ((root.monitor.battery !== null && root.monitor.battery !== undefined ? "▮ " + root.monitor.battery + " %   " : "") + (root.monitor.model || "Monitor") + (root.monitor.firmware ? " · fw " + root.monitor.firmware : ""))
                  : "No monitor trusted yet"
                color: root.ink2
                font.family: root.family
                font.pixelSize: Style.font.caption
              }
              Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                readonly property bool off: root.summary.clock_skew_minutes !== null && root.summary.clock_skew_minutes !== undefined
                text: off ? "Clock " + root.summary.clock_skew_minutes + " min off" : (root.summary.clock_checked ? "Clock checked" : "Clock not checked")
                color: off ? root.ink : root.ink2
                font.family: root.family
                font.pixelSize: Style.font.caption
                font.weight: off ? Font.Bold : Font.Normal
              }
            }
            Text {
              visible: root.summary.clock_skew_minutes !== null && root.summary.clock_skew_minutes !== undefined
              width: parent.width
              wrapMode: Text.WordWrap
              text: "Set the cuff's clock from its own menu; the next sync checks it again."
              color: root.ink2
              font.family: root.family
              font.pixelSize: Style.font.caption
            }
          }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "For personal records only. Not a medical device and not medical advice; see a clinician about your readings."
            color: root.ink2
            font.family: root.family
            font.pixelSize: Style.font.caption
          }

          // Copy image
          Item {
            visible: !root.capturing
            width: parent.width
            height: copyButton.height
            ActionButton {
              id: copyButton
              anchors.right: parent.right
              text: root.copied ? "Copied to clipboard" : "Copy image"
              ink: root.ink
              fontFamily: root.family
              onClicked: root.copyImage()
            }
          }
        }
        }
      }
    }
  }

  Process {
    id: enqueue
    stdinEnabled: false
  }

  Process {
    id: clipboard
    stdinEnabled: false
  }

  Timer {
    id: copiedReset
    interval: 2500
    onTriggered: root.copied = false
  }

  FileView {
    path: root.stateDir + "/status.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var doc
      try { doc = JSON.parse(text() || "{}") } catch (error) { return }
      var previous = root.linkState
      root.linkState = doc.state || "idle"
      if (previous !== root.linkState && root.opened) root.askSummary()
    }
  }

  FileView {
    path: root.stateDir + "/response.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.applyResponse(text())
  }

  // A response that never arrives (sidecar restarting) must not wedge the queue.
  Timer {
    id: flushGuard
    interval: 4000
    onTriggered: { root.inflight = ""; root.dispatch() }
  }

  Timer {
    interval: 5000
    repeat: true
    running: root.opened && !root.editing
    onTriggered: root.askSummary()
  }
}
