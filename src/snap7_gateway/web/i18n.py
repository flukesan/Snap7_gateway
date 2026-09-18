"""Bilingual (English / Thai) text for the operator-facing web UI.

Per the project brief, code, docstrings and the on-disk log file stay English -
they are read by engineers and by tooling - while everything an operator sees
in the browser, including status text and log severity labels, follows the
``locale`` setting.

Adding a language means adding a column to :data:`CATALOG`; a missing key falls
back to English and then to the key itself, so a partially translated catalog
degrades gracefully instead of rendering blanks.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "th")

LOCALE_NAMES = {"en": "English", "th": "ไทย"}

CATALOG: dict[str, dict[str, str]] = {
    # --- chrome -------------------------------------------------------
    "app.title": {"en": "Snap7 Industrial Gateway", "th": "Snap7 Industrial Gateway"},
    "nav.status": {"en": "System Status", "th": "สถานะระบบ"},
    "nav.connections": {"en": "PLC Connections", "th": "การเชื่อมต่อ PLC"},
    "nav.tags": {"en": "Tag Mapping", "th": "การแมปแท็ก"},
    "nav.devicewise": {"en": "Device / Output Side", "th": "ฝั่งอุปกรณ์ปลายทาง"},
    "nav.tools": {"en": "Test Tools", "th": "เครื่องมือทดสอบ"},
    "nav.users": {"en": "Users & Security", "th": "ผู้ใช้และความปลอดภัย"},
    "nav.logs": {"en": "Logs", "th": "บันทึกเหตุการณ์"},
    "nav.audit": {"en": "Audit Trail", "th": "ประวัติการแก้ไข"},
    "nav.logout": {"en": "Log out", "th": "ออกจากระบบ"},
    "nav.signed_in_as": {"en": "Signed in as", "th": "เข้าสู่ระบบในชื่อ"},
    # --- generic ------------------------------------------------------
    "action.save": {"en": "Save", "th": "บันทึก"},
    "action.cancel": {"en": "Cancel", "th": "ยกเลิก"},
    "action.add": {"en": "Add", "th": "เพิ่ม"},
    "action.edit": {"en": "Edit", "th": "แก้ไข"},
    "action.delete": {"en": "Delete", "th": "ลบ"},
    "action.test": {"en": "Test Connection", "th": "ทดสอบการเชื่อมต่อ"},
    "action.rescan": {"en": "Rescan", "th": "สแกนใหม่"},
    "action.refresh": {"en": "Refresh", "th": "โหลดใหม่"},
    "action.download": {"en": "Download", "th": "ดาวน์โหลด"},
    "action.expose": {"en": "Expose", "th": "เปิดเผย"},
    "action.hide": {"en": "Hide", "th": "ซ่อน"},
    "action.confirm_shape": {"en": "Confirm new size", "th": "ยืนยันขนาดใหม่"},
    "action.read": {"en": "Read", "th": "อ่านค่า"},
    "action.login": {"en": "Log in", "th": "เข้าสู่ระบบ"},
    "action.change_password": {"en": "Change password", "th": "เปลี่ยนรหัสผ่าน"},
    "action.apply": {"en": "Apply", "th": "นำไปใช้"},
    "common.yes": {"en": "Yes", "th": "ใช่"},
    "common.no": {"en": "No", "th": "ไม่"},
    "common.none": {"en": "None", "th": "ไม่มี"},
    "common.never": {"en": "Never", "th": "ยังไม่เคย"},
    "common.name": {"en": "Name", "th": "ชื่อ"},
    "common.status": {"en": "Status", "th": "สถานะ"},
    "common.size": {"en": "Size (bytes)", "th": "ขนาด (ไบต์)"},
    "common.actions": {"en": "Actions", "th": "การทำงาน"},
    "common.enabled": {"en": "Enabled", "th": "เปิดใช้งาน"},
    "common.address": {"en": "Address", "th": "แอดเดรส"},
    "common.value": {"en": "Value", "th": "ค่า"},
    "common.type": {"en": "Data type", "th": "ชนิดข้อมูล"},
    "common.time": {"en": "Time", "th": "เวลา"},
    "common.level": {"en": "Level", "th": "ระดับ"},
    "common.message": {"en": "Message", "th": "ข้อความ"},
    "common.module": {"en": "Module", "th": "โมดูล"},
    "common.user": {"en": "User", "th": "ผู้ใช้"},
    "common.required": {"en": "required", "th": "จำเป็น"},
    "common.optional": {"en": "optional", "th": "ไม่บังคับ"},
    # --- connection states -------------------------------------------
    "state.connected": {"en": "Connected", "th": "เชื่อมต่อแล้ว"},
    "state.connecting": {"en": "Connecting", "th": "กำลังเชื่อมต่อ"},
    "state.reconnecting": {"en": "Reconnecting", "th": "กำลังเชื่อมต่อใหม่"},
    "state.failed": {"en": "Failed", "th": "ล้มเหลว"},
    "state.disabled": {"en": "Disabled", "th": "ปิดใช้งาน"},
    "state.stopped": {"en": "Stopped", "th": "หยุดทำงาน"},
    # --- area status --------------------------------------------------
    "areastatus.new": {"en": "New (not yet exposed)", "th": "ใหม่ (ยังไม่เปิดเผย)"},
    "areastatus.ok": {"en": "OK", "th": "ปกติ"},
    "areastatus.grown": {"en": "Larger than before", "th": "ขนาดใหญ่ขึ้น"},
    "areastatus.shrunk": {"en": "Smaller than before", "th": "ขนาดเล็กลง"},
    "areastatus.missing": {"en": "Missing on the PLC", "th": "ไม่พบบน PLC"},
    # --- login / password --------------------------------------------
    "login.heading": {"en": "Gateway sign-in", "th": "เข้าสู่ระบบเกตเวย์"},
    "login.username": {"en": "Username", "th": "ชื่อผู้ใช้"},
    "login.password": {"en": "Password", "th": "รหัสผ่าน"},
    "login.expired": {
        "en": "This sign-in page had been open too long. Your details were not sent - "
              "please enter them again.",
        "th": "หน้าเข้าสู่ระบบนี้เปิดทิ้งไว้นานเกินไป ระบบยังไม่ได้ส่งข้อมูลของคุณ "
              "กรุณากรอกใหม่อีกครั้ง",
    },
    "login.default_active": {
        "en": "This gateway is still using its initial password. Sign in and change it "
              "now - until you do, anyone who can reach this address can take it over.",
        "th": "เกตเวย์นี้ยังใช้รหัสผ่านเริ่มต้นอยู่ กรุณาเข้าสู่ระบบและเปลี่ยนรหัสผ่านทันที "
              "ระหว่างนี้ผู้ที่เข้าถึงแอดเดรสนี้ได้สามารถยึดเครื่องไปได้",
    },
    "pwchange.heading": {"en": "Change your password", "th": "เปลี่ยนรหัสผ่านของคุณ"},
    "pwchange.forced": {
        "en": "Your password must be changed before you can use the gateway.",
        "th": "คุณต้องเปลี่ยนรหัสผ่านก่อนจึงจะใช้งานเกตเวย์ได้",
    },
    "pwchange.current": {"en": "Current password", "th": "รหัสผ่านปัจจุบัน"},
    "pwchange.new": {"en": "New password", "th": "รหัสผ่านใหม่"},
    "pwchange.confirm": {"en": "Confirm new password", "th": "ยืนยันรหัสผ่านใหม่"},
    "pwchange.rules": {"en": "Password rules", "th": "ข้อกำหนดรหัสผ่าน"},
    "pwchange.success": {
        "en": "Password changed. Please sign in again.",
        "th": "เปลี่ยนรหัสผ่านแล้ว กรุณาเข้าสู่ระบบอีกครั้ง",
    },
    # --- connections --------------------------------------------------
    "conn.heading": {"en": "PLC Connections", "th": "การเชื่อมต่อ PLC"},
    "conn.host": {"en": "PLC address", "th": "แอดเดรส PLC"},
    "conn.rack": {"en": "Rack", "th": "แร็ก"},
    "conn.slot": {"en": "Slot", "th": "สล็อต"},
    "conn.port": {"en": "TCP port", "th": "พอร์ต TCP"},
    "conn.type": {"en": "Connection type", "th": "ชนิดการเชื่อมต่อ"},
    "conn.timeout": {"en": "Timeout (ms)", "th": "หมดเวลา (มิลลิวินาที)"},
    "conn.poll": {"en": "Poll interval (ms)", "th": "รอบการอ่าน (มิลลิวินาที)"},
    "conn.exposure": {"en": "Exposure mode", "th": "โหมดการเปิดเผยข้อมูล"},
    "conn.exposure.mirror_all": {"en": "Mirror All", "th": "มิเรอร์ทั้งหมด"},
    "conn.exposure.whitelist": {"en": "Whitelist only", "th": "เฉพาะที่อนุญาต"},
    "conn.write_enabled": {"en": "Allow writes to this PLC", "th": "อนุญาตให้เขียนค่าไปยัง PLC นี้"},
    "conn.read_areas": {"en": "Areas to read", "th": "พื้นที่ที่จะอ่าน"},
    "conn.last_success": {"en": "Last successful read", "th": "อ่านสำเร็จล่าสุด"},
    "conn.created": {"en": "Connection saved.", "th": "บันทึกการเชื่อมต่อแล้ว"},
    "conn.updated": {"en": "Connection updated.", "th": "อัปเดตการเชื่อมต่อแล้ว"},
    "conn.deleted": {"en": "Connection deleted.", "th": "ลบการเชื่อมต่อแล้ว"},
    "conn.none": {
        "en": "No PLC connections are configured yet.",
        "th": "ยังไม่มีการตั้งค่าการเชื่อมต่อ PLC",
    },
    # --- tags / areas -------------------------------------------------
    "tags.heading": {"en": "Tag Mapping", "th": "การแมปแท็ก"},
    "tags.areas": {"en": "Discovered areas", "th": "พื้นที่หน่วยความจำที่ค้นพบ"},
    "tags.named": {"en": "Named tags", "th": "แท็กที่ตั้งชื่อไว้"},
    "tags.exposed": {"en": "Exposed to DeviceWise", "th": "เปิดเผยให้ DeviceWise"},
    "tags.rescan_queued": {
        "en": "Rescan queued for {count} connection(s).",
        "th": "สั่งสแกนใหม่แล้วสำหรับ {count} การเชื่อมต่อ",
    },
    "tags.none": {
        "en": "Nothing discovered yet. Connect a PLC and run a rescan.",
        "th": "ยังไม่พบข้อมูล เชื่อมต่อ PLC แล้วสั่งสแกนใหม่",
    },
    # --- devicewise ---------------------------------------------------
    "dw.heading": {"en": "Device / Output Side (DeviceWise)", "th": "ฝั่งอุปกรณ์ปลายทาง (DeviceWise)"},
    "dw.explain": {
        "en": "The gateway hosts a virtual S7 CPU. Point the DeviceWise Siemens driver "
              "(Connection: Direct) at this address instead of the NetLink-Pro box.",
        "th": "เกตเวย์จำลองตัวเองเป็น S7 CPU ให้ตั้งค่าไดรเวอร์ Siemens ของ DeviceWise "
              "(Connection: Direct) ให้ชี้มาที่แอดเดรสนี้แทนกล่อง NetLink-Pro",
    },
    "dw.registered": {"en": "Registered areas", "th": "พื้นที่ที่ลงทะเบียนแล้ว"},
    # --- logs ---------------------------------------------------------
    "logs.heading": {"en": "Logs", "th": "บันทึกเหตุการณ์"},
    "logs.filter": {"en": "Minimum severity", "th": "ระดับต่ำสุดที่แสดง"},
    "logs.empty": {"en": "No log entries match this filter.", "th": "ไม่มีบันทึกที่ตรงกับตัวกรอง"},
    # --- status -------------------------------------------------------
    "status.heading": {"en": "System Status", "th": "สถานะระบบ"},
    "status.uptime": {"en": "Uptime", "th": "เวลาทำงาน"},
    "status.vplc": {"en": "Virtual S7 CPU", "th": "S7 CPU เสมือน"},
    "status.crash": {"en": "Crash diagnostics", "th": "ข้อมูลวินิจฉัยการล่ม"},
    "status.unclean": {
        "en": "The previous run did not shut down cleanly. A crash snapshot was saved.",
        "th": "การทำงานครั้งก่อนปิดตัวลงผิดปกติ ระบบได้บันทึกสแนปช็อตไว้แล้ว",
    },
    "status.no_crash": {"en": "No crash snapshots recorded.", "th": "ไม่มีสแนปช็อตการล่ม"},
    # --- messages -----------------------------------------------------
    "msg.saved": {"en": "Settings saved.", "th": "บันทึกการตั้งค่าแล้ว"},
    "msg.invalid": {"en": "Please correct the errors below.", "th": "กรุณาแก้ไขข้อผิดพลาดด้านล่าง"},
    "msg.forbidden": {
        "en": "Your account does not have permission for that action.",
        "th": "บัญชีของคุณไม่มีสิทธิ์ทำรายการนี้",
    },
    "msg.csrf": {
        "en": "Your session expired or the request could not be verified. Please try again.",
        "th": "เซสชันหมดอายุหรือไม่สามารถตรวจสอบคำขอได้ กรุณาลองใหม่",
    },
    "msg.not_found": {"en": "That item no longer exists.", "th": "ไม่พบรายการนี้แล้ว"},
}


class Translator:
    """Resolves message keys for one locale."""

    __slots__ = ("locale",)

    def __init__(self, locale: str | None = None) -> None:
        candidate = (locale or DEFAULT_LOCALE).lower()
        self.locale = candidate if candidate in SUPPORTED_LOCALES else DEFAULT_LOCALE

    def __call__(self, key: str, **params: Any) -> str:
        return self.gettext(key, **params)

    def gettext(self, key: str, **params: Any) -> str:
        entry = CATALOG.get(key)
        if entry is None:
            text = key
        else:
            text = entry.get(self.locale) or entry.get(DEFAULT_LOCALE) or key
        if params:
            try:
                return text.format(**params)
            except (KeyError, IndexError):
                return text
        return text

    def state(self, state: str) -> str:
        return self.gettext(f"state.{state}")

    def area_status(self, status: str) -> str:
        return self.gettext(f"areastatus.{status}")
