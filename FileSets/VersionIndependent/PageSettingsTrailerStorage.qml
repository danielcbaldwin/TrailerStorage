//////// TrailerStorage settings page (added by the TrailerStorage package)

import QtQuick 2
import "utils.js" as Utils
import com.victron.velib 1.0

MbPage {
	id: root
	title: qsTr("Trailer storage")

	property string settingsPath: "com.victronenergy.settings/Settings/TrailerStorage"
	property string servicePath: "com.victronenergy.trailerstorage"
	property VBusItem stateItem: VBusItem { bind: Utils.path(servicePath, "/State") }
	property VBusItem enabledItem: VBusItem { bind: Utils.path(settingsPath, "/Enabled") }
	property VBusItem forceFullItem: VBusItem { bind: Utils.path(settingsPath, "/ForceFullCharge") }
	property bool running: stateItem.valid

	model: VisibleItemModel {
		MbItemText {
			text: qsTr("TrailerStorage service is not running")
			wrapMode: Text.WordWrap
			horizontalAlignment: Text.AlignLeft
			show: !root.running
		}

		MbSwitch {
			id: enableSwitch
			name: qsTr("Storage mode")
			bind: Utils.path(root.settingsPath, "/Enabled")
			writeAccessLevel: User.AccessUser
		}

		MbItemValue {
			description: qsTr("Status")
			item.bind: Utils.path(root.servicePath, "/StatusText")
			show: root.running
		}

		MbItemValue {
			description: qsTr("Battery SOC")
			item {
				bind: Utils.path(root.servicePath, "/Soc")
				unit: "%"
				decimals: 1
			}
			show: root.running
		}

		MbItemValue {
			description: qsTr("Charge current limit")
			item {
				bind: Utils.path(root.servicePath, "/ChargeLimit")
				unit: "A"
				decimals: 0
			}
			show: root.running && enableSwitch.checked
		}

		MbSpinBox {
			description: qsTr("Floor (start charging)")
			writeAccessLevel: User.AccessUser
			item {
				bind: Utils.path(root.settingsPath, "/Floor")
				unit: "%"
				decimals: 0
				step: 1
				min: 10
				max: 95
			}
		}

		MbSpinBox {
			description: qsTr("Ceiling (stop charging)")
			writeAccessLevel: User.AccessUser
			item {
				bind: Utils.path(root.settingsPath, "/Ceiling")
				unit: "%"
				decimals: 0
				step: 1
				min: 15
				max: 100
			}
		}

		MbSpinBox {
			description: qsTr("Full charge every (0 = never)")
			writeAccessLevel: User.AccessUser
			item {
				bind: Utils.path(root.settingsPath, "/FullChargeDays")
				unit: qsTr("days")
				decimals: 0
				step: 1
				min: 0
				max: 365
			}
		}

		MbOK {
			description: qsTr("Full charge now")
			value: root.forceFullItem.value === 1 ? qsTr("Requested") : qsTr("Press to start")
			writeAccessLevel: User.AccessUser
			show: root.running && enableSwitch.checked
			onClicked: {
				root.forceFullItem.setValue(1)
				toast.createToast(qsTr("Full charge requested"), 5000)
			}
		}

		MbItemValue {
			description: qsTr("Last full charge")
			item.bind: Utils.path(root.servicePath, "/FullChargeResult")
			show: root.running
		}

		MbItemValue {
			description: qsTr("Last exit reason")
			item.bind: Utils.path(root.servicePath, "/LastExitReason")
			show: root.running
		}

		MbItemText {
			text: qsTr("While storage mode is on, this service controls DVCC and the charge current limit. It turns itself off when shore power is lost and restores the normal settings.")
			wrapMode: Text.WordWrap
			horizontalAlignment: Text.AlignLeft
		}
	}
}
