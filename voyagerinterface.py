#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 José González Divassón
#
# VoyagerInterface for Linux: read and write the 2 KB Continuous Memory of an HP 12C / 15C /
# 16C CE over USB (calculator off, hold f and ENTER, press ON). Python 3 + Tkinter, talks to
# /dev/hidraw directly (no extra libraries).
#
# Protocol (CE firmware HID handler): 64-byte reports, VID 03F0, PID 1341, report ID 0.
#   Output [0xFE, 0...]                 -> calculator sends its 2,048-byte image as input
#                                          reports [n, n bytes], n < 64.
#   Output [34, 'C', block, 32 bytes]   -> copies the bytes to the image at block*32, at once,
#                                          into the running calculator. No reply.
# Write rules (VoyagerCalc 0.6.x, tested on a real calculator):
#   - image 0x00..0x6F is the calculator's live CPU state (power-state byte at 0x30): keep the
#     calculator's own bytes, or it stops mid-transfer and drops off USB.
#   - 0x7F0..0x7FF is a signature and live counters: keep the calculator's own bytes.
#   - only 0x70..0x7EF (registers, program) comes from the file; HP-15C flag 8 (byte 0x4C bit 3,
#     copy in 0xC4 bit 0) also comes from the file.
#   - send only changed 32-byte blocks, read back, compare 0x70..0x7EF, retry up to 3 times.

import datetime
import glob
import os
import select
import sys
import threading
import time

VERSION = "0.1.1"
VID, PID = 0x03F0, 0x1341
REPORT, MEMSIZE, BLOCK = 64, 2048, 32
DUMP_CMD, WRITE_CMD = 0xFE, 0x43
MEM_RANGE = (0x70, 0x7F0)
FLAG8_OFF, FLAG8_MASK, S0A_OFF = 0x4C, 0x08, 0xC4
SIG_RANGE = (0x7F0, 0x7F8)
BACKUP_DIR = os.path.join(os.path.expanduser("~"), "VoyagerInterface Backups")
CONNECT_HINT = ("Connect the calculator by USB, turn it off, hold f and ENTER and press ON.\n"
                "In Parallels, connect the new USB device to Linux (not to the Mac).")


class LinkError(Exception):
    pass


class NoDevice(LinkError):
    def __init__(self, msg=None):
        super().__init__(msg or "No calculator found.\n" + CONNECT_HINT)


# ---------------------------------------------------------------- log

class Log:
    path = None

    @classmethod
    def write(cls, line):
        if not cls.path:
            return
        try:
            os.makedirs(os.path.dirname(cls.path), exist_ok=True)
            with open(cls.path, "a") as f:
                f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "  " + line + "\n")
        except OSError:
            pass


def hexs(b):
    b = bytes(b)
    t = b.rstrip(b"\0")
    z = len(b) - len(t)
    return t.hex(" ").upper() + (" +%dx00" % z if z else "")


# ---------------------------------------------------------------- hidraw transport

def find_hidraw(sysfs="/sys/class/hidraw"):
    """Return /dev/hidrawN of the calculator, or None."""
    want = "HID_ID=0003:%08X:%08X" % (VID, PID)
    for d in sorted(glob.glob(os.path.join(sysfs, "hidraw*"))):
        try:
            with open(os.path.join(d, "device", "uevent")) as f:
                if any(l.strip().upper() == want for l in f):
                    return "/dev/" + os.path.basename(d)
        except OSError:
            continue
    return None


class HidrawTransport:
    """One calculator on /dev/hidrawN. Writes carry the leading report ID 0 (65 bytes)."""

    def __init__(self, path=None, fd=None):
        self.path, self.fd, self._given_fd = path, fd, fd is not None

    def open(self):
        if self._given_fd:
            return
        self.close()
        p = self.path or find_hidraw()
        if not p:
            raise NoDevice()
        try:
            self.fd = os.open(p, os.O_RDWR)
        except PermissionError:
            raise LinkError("Linux does not allow access to %s.\nRun install.sh once (it installs the "
                            "USB rule), then unplug and reconnect the calculator." % p)
        except OSError as e:
            raise NoDevice("The calculator went away (%s).\n%s" % (e.strerror, CONNECT_HINT))
        Log.write("open %s" % p)
        self.flush()

    def close(self):
        if self.fd is not None and not self._given_fd:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def flush(self):
        while self.receive(0) is not None:
            pass

    def send(self, report):
        assert len(report) == REPORT
        try:
            n = os.write(self.fd, bytes([0]) + bytes(report))
        except OSError as e:
            Log.write("send failed: %s" % e)
            raise LinkError("Sending to the calculator failed (%s). It may have left USB mode." % e.strerror)
        Log.write("send %d: %s" % (n, hexs(report)))

    def receive(self, timeout):
        r, _, _ = select.select([self.fd], [], [], max(0, timeout))
        if not r:
            return None
        try:
            b = os.read(self.fd, REPORT + 1)
        except OSError as e:
            raise LinkError("Reading from the calculator failed (%s)." % e.strerror)
        if not b:
            raise NoDevice("The calculator went away.\n" + CONNECT_HINT)
        Log.write("recv %d: %s" % (len(b), hexs(b)))
        return b


# ---------------------------------------------------------------- protocol + write rules

def dump_report():
    r = bytearray(REPORT)
    r[0] = DUMP_CMD
    return bytes(r)


def write_reports(image):
    if len(image) != MEMSIZE:
        raise LinkError("A memory image is %d bytes, not %d." % (MEMSIZE, len(image)))
    out = []
    for b in range(MEMSIZE // BLOCK):
        r = bytearray(REPORT)
        r[0], r[1], r[2] = BLOCK + 2, WRITE_CMD, b
        r[3:3 + BLOCK] = image[b * BLOCK:(b + 1) * BLOCK]
        out.append(bytes(r))
    return out


def flag8(image):
    return bool(image[FLAG8_OFF] & FLAG8_MASK)


def merge(data, current, with_flag8=None):
    """Calculator's own bytes outside MEM_RANGE, the file's inside it (+ 15C flag 8)."""
    out = bytearray(current)
    a, b = MEM_RANGE
    out[a:b] = data[a:b]
    if with_flag8 is not None:
        if with_flag8:
            out[FLAG8_OFF] |= FLAG8_MASK
            out[S0A_OFF] |= 1
        else:
            out[FLAG8_OFF] &= ~FLAG8_MASK & 0xFF
            out[S0A_OFF] &= 0xFE
    return bytes(out)


def compare(a, b):
    d = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    return (len(d), d[0]) if d else None


class Link:
    def __init__(self, transport, read_timeout=3.0, command_pause=0.1, packet_pause=0.001, attempts=3):
        self.t = transport
        self.read_timeout, self.command_pause, self.packet_pause, self.attempts = \
            read_timeout, command_pause, packet_pause, attempts

    def _dump(self):
        self.t.send(dump_report())
        if self.command_pause:
            time.sleep(self.command_pause)
        d = bytearray()
        end = time.monotonic() + self.read_timeout
        while time.monotonic() < end:
            r = self.t.receive(min(0.25, max(0.01, end - time.monotonic())))
            if r is None:
                continue
            n = r[0]
            if n == 0:
                continue
            if n >= REPORT or n >= len(r) or len(d) + n > MEMSIZE:
                raise LinkError("The calculator sent an unexpected USB report (length byte %d)." % n)
            d += r[1:1 + n]
            if len(d) == MEMSIZE:
                return bytes(d)
        raise LinkError("The calculator stopped answering (%d of %d bytes received).\n%s"
                        % (len(d), MEMSIZE, CONNECT_HINT))

    def read_memory(self):
        self.t.open()
        try:
            return self._dump()
        finally:
            self.t.close()

    def write_memory(self, data, with_flag8=None, before_write=None):
        """before_write(current_image) is called with the pre-write read (the backup) before
        anything is sent. Returns the verified read-back."""
        if len(data) != MEMSIZE:
            raise LinkError("A memory image is %d bytes, not %d." % (MEMSIZE, len(data)))
        self.t.open()
        try:
            current = self._dump()
            if before_write:
                before_write(current)
            target = merge(data, current, with_flag8)
            reports = write_reports(target)
            a, b = MEM_RANGE
            last = LinkError("Write failed.")
            for attempt in range(max(1, self.attempts)):
                sent = 0
                for i, r in enumerate(reports):
                    rng = slice(i * BLOCK, (i + 1) * BLOCK)
                    if current[rng] == target[rng]:
                        continue
                    self.t.send(r)
                    sent += 1
                    if self.packet_pause:
                        time.sleep(self.packet_pause)
                Log.write("attempt %d: sent %d blocks" % (attempt + 1, sent))
                try:
                    back = self._dump()
                except NoDevice:
                    raise
                except LinkError as e:
                    last = e
                    continue
                current = back
                c = compare(back[a:b], target[a:b])
                if c:
                    last = LinkError("Read-back check failed: %d bytes differ (first at 0x%03X)." % (c[0], c[1] + a))
                    continue
                if with_flag8 is not None and flag8(back) != with_flag8:
                    last = LinkError("Read-back check failed: flag 8 did not change.")
                    continue
                return back
            raise last
        finally:
            self.t.close()


# ---------------------------------------------------------------- warnings (never block)

def guess_model_from_name(path):
    ext = os.path.splitext(path)[1].lower()
    return {".12c": "12C", ".15c": "15C", ".16c": "16C"}.get(ext)


def flag8_for_write(path, data):
    """HP-15C flag 8 to carry from the file into the calculator's CPU state, or None (keep the
    calculator's). 0.1.0 only did this for *.15c names, so a file saved by "Read" (*.bin) lost
    flag 8 when written to a calculator that had it off (bug B). Now: always from the file,
    except for files named as 12C / 16C memories."""
    if guess_model_from_name(path) in ("12C", "16C"):
        return None
    return flag8(data)


def regs_used(image):
    """(nonzero registers at the bottom of the user area, program registers at the top)."""
    regs = [any(image[i:i + 8]) for i in range(MEM_RANGE[0], MEM_RANGE[1], 8)]  # 240 registers
    top = 0
    for used in reversed(regs):
        if not used:
            break
        top += 1
    return sum(regs[48:len(regs) - top]), top


def warnings_for(data, current, file_path):
    """Plain-language warnings for a write. They never stop the write."""
    w = []
    if data[SIG_RANGE[0]:SIG_RANGE[1]] != current[SIG_RANGE[0]:SIG_RANGE[1]]:
        w.append("The file looks like it comes from a different calculator model or firmware than "
                 "the connected calculator. The calculator may show \"Pr Error\" and clear its memory.")
    model = guess_model_from_name(file_path)
    if model not in ("12C", "16C"):
        data_regs, prog_regs = regs_used(data)
        if prog_regs >= 60 or data_regs + prog_regs > 90:
            w.append("%sThis memory is large (about %d program registers). It probably needs the HP-15C in "
                     "15.2 mode (double memory). Set the calculator to 15.2 first (off, g-ENTER-ON, 4), or "
                     "it may show Error 10 at g MEM." % ("" if model else "If this is an HP-15C memory: ", prog_regs))
        elif model == "15C":
            w.append("HP-15C: the calculator and the file should use the same mode (15.0 or 15.2).")
    return w


RULE_PATH = "/etc/udev/rules.d/99-voyagerinterface.rules"
RULE = ('KERNEL=="hidraw*", ATTRS{idVendor}=="03f0", ATTRS{idProduct}=="1341", '
        'MODE="0660", GROUP="plugdev", TAG+="uaccess"')


def fix_script(user, dev=None):
    """Shell script (run as root through pkexec) that does what install.sh does for access."""
    s = ("set -e\n"
         "printf '%%s\\n' '# VoyagerInterface: HP 12C/15C/16C CE in USB memory mode (03F0:1341)' '%s' > %s\n"
         "chmod 0644 %s\n"
         "getent group plugdev >/dev/null || groupadd plugdev\n"
         "id -nG %s | tr ' ' '\\n' | grep -qx plugdev || usermod -aG plugdev %s\n"
         "udevadm control --reload-rules\n"
         "udevadm trigger --subsystem-match=hidraw --action=change || true\n"
         % (RULE, RULE_PATH, RULE_PATH, user, user))
    if dev:
        s += "chgrp plugdev %s && chmod 0660 %s\n" % (dev, dev)
        s += "command -v setfacl >/dev/null && setfacl -m u:%s:rw %s || true\n" % (user, dev)
    return s


def backup_name(kind, ext=".bin"):
    return os.path.join(BACKUP_DIR, "calculator-%s-%s%s" % (kind, datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S"), ext))


def save_backup(image, kind):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    p = backup_name(kind)
    with open(p, "wb") as f:
        f.write(image)
    return p


# ---------------------------------------------------------------- window

def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    Log.path = os.path.join(BACKUP_DIR, "usb.log")
    root = tk.Tk()
    root.title("VoyagerInterface %s" % VERSION)
    root.geometry("560x380")
    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="VoyagerInterface", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Back up and restore the memory of an HP 12C / 15C / 16C CE over USB.\n"
                        "Unofficial tool, use at your own risk.").pack(anchor="w", pady=(0, 10))
    status = tk.StringVar()
    st = ttk.Label(frm, textvariable=status, wraplength=520, justify="left")
    st.pack(anchor="w", fill="x", pady=(0, 10))

    btns = ttk.Frame(frm)
    btns.pack(fill="x")
    bread = ttk.Button(btns, text="Read calculator to file…")
    bwrite = ttk.Button(btns, text="Write file to calculator…")
    bcheck = ttk.Button(btns, text="Check connection")
    bfolder = ttk.Button(btns, text="Open backups folder")
    for i, b in enumerate((bread, bwrite, bcheck, bfolder)):
        b.grid(row=i // 2, column=i % 2, sticky="ew", padx=4, pady=4, ipady=6)
    btns.columnconfigure(0, weight=1)
    btns.columnconfigure(1, weight=1)

    msg = tk.StringVar(value="")
    ttk.Label(frm, textvariable=msg, wraplength=520, justify="left").pack(anchor="w", pady=(12, 0))
    ttk.Label(frm, text="Backups: " + BACKUP_DIR, foreground="gray").pack(side="bottom", anchor="w")

    busy = []

    def set_busy(b):
        for w in (bread, bwrite, bcheck):
            w.state(["disabled"] if b else ["!disabled"])

    bfix = ttk.Button(frm, text="Fix permissions (asks for your password)")

    def check():
        p = find_hidraw()
        denied = bool(p) and not os.access(p, os.R_OK | os.W_OK)
        if p:
            status.set("Calculator found (%s)." % p if not denied else
                       "Calculator found (%s), but Linux does not allow access. Click \"Fix permissions\"." % p)
        else:
            status.set("No calculator found. " + CONNECT_HINT)
        if denied:
            if not bfix.winfo_ismapped():
                bfix.pack(anchor="w", pady=(8, 0), ipady=4)
        elif bfix.winfo_ismapped():
            bfix.pack_forget()

    def fix_permissions():
        import getpass
        import shutil
        import subprocess
        user, dev = getpass.getuser(), find_hidraw()
        if not shutil.which("pkexec"):
            messagebox.showerror("VoyagerInterface", "This system cannot ask for the password from here.\n"
                                 "Open the VoyagerInterface folder, right-click > Open Terminal Here, "
                                 "type:  sh install.sh  and press Enter.")
            return
        r = subprocess.run(["pkexec", "/bin/sh", "-c", fix_script(user, dev)], capture_output=True, text=True)
        if r.returncode != 0:
            messagebox.showerror("VoyagerInterface", "Fixing the permissions did not work (password cancelled?).\n"
                                 "Alternative: in the VoyagerInterface folder, right-click > Open Terminal Here, "
                                 "type:  sh install.sh  and press Enter.\n\n" + (r.stderr or "")[-400:])
            return
        time.sleep(1)
        check()
        if dev and os.access(dev, os.R_OK | os.W_OK):
            messagebox.showinfo("VoyagerInterface", "Fixed. You can read and write the calculator now.")
        else:
            messagebox.showinfo("VoyagerInterface", "Fixed for the next time. Please log out and log in once "
                                "(or restart the virtual machine), then start VoyagerInterface again.")

    def run(work, done):
        if busy:
            return
        busy.append(1)
        set_busy(True)

        def th():
            try:
                res, err = work(), None
            except LinkError as e:
                res, err = None, e
            except Exception as e:  # unexpected
                res, err = None, LinkError("Unexpected error: %r" % e)

            def fin():
                busy.clear()
                set_busy(False)
                check()
                if err:
                    msg.set("")
                    messagebox.showerror("VoyagerInterface", str(err))
                else:
                    done(res)
            root.after(0, fin)
        threading.Thread(target=th, daemon=True).start()

    def do_read():
        msg.set("Reading the calculator…")

        def done(img):
            p = save_backup(img, "read")
            msg.set("Read OK. Saved to:\n" + p)
            if messagebox.askyesno("VoyagerInterface", "Read OK, saved to the backups folder:\n%s\n\n"
                                   "Also save a copy somewhere else?" % os.path.basename(p)):
                q = filedialog.asksaveasfilename(initialdir=os.path.expanduser("~"), initialfile=os.path.basename(p),
                                                 filetypes=[("Memory image", "*.bin *.12c *.15c *.16c"), ("All files", "*")])
                if q:
                    with open(q, "wb") as f:
                        f.write(img)
                    msg.set("Read OK. Saved to:\n%s\nand %s" % (p, q))
        run(lambda: Link(HidrawTransport()).read_memory(), done)

    def do_write():
        path = filedialog.askopenfilename(initialdir=BACKUP_DIR if os.path.isdir(BACKUP_DIR) else os.path.expanduser("~"),
                                          filetypes=[("Memory image", "*.bin *.12c *.15c *.16c"), ("All files", "*")])
        if not path:
            return
        with open(path, "rb") as f:
            data = f.read()
        if len(data) != MEMSIZE:
            messagebox.showerror("VoyagerInterface", "This file is %d bytes. A calculator memory file is exactly 2048 bytes." % len(data))
            return
        if not messagebox.askokcancel("VoyagerInterface", "Write\n%s\nto the calculator?\n\nThe calculator's current memory "
                                      "is saved to the backups folder first." % os.path.basename(path), icon="warning"):
            return
        f8 = flag8_for_write(path, data)
        info = {}

        def work():
            link = Link(HidrawTransport())
            # 1st pass: read and back up, collect warnings
            cur = link.read_memory()
            info["backup"] = save_backup(cur, "before-write")
            info["warn"] = warnings_for(data, cur, path)
            return cur

        def after_backup(cur):
            msg.set("Backup saved: " + info["backup"])
            if info["warn"] and not messagebox.askokcancel(
                    "VoyagerInterface – warning", "\n\n".join(info["warn"]) + "\n\nWrite anyway?", icon="warning"):
                msg.set("Not written. Backup saved: " + info["backup"])
                return
            msg.set("Writing…")

            def w2():
                return Link(HidrawTransport()).write_memory(data, with_flag8=f8)
            run(w2, lambda back: (msg.set("Written and verified.\nBackup of the old memory: " + info["backup"]),
                                  messagebox.showinfo("VoyagerInterface", "Written and verified.\n\nIf the calculator is still "
                                                      "in USB mode, turn it off and on.\nTo undo, write this backup back:\n"
                                                      + os.path.basename(info["backup"]))))
        msg.set("Reading the calculator (backup)…")
        run(work, after_backup)

    def open_folder():
        os.makedirs(BACKUP_DIR, exist_ok=True)
        os.spawnlp(os.P_NOWAIT, "xdg-open", "xdg-open", BACKUP_DIR)

    bread.configure(command=do_read)
    bwrite.configure(command=do_write)
    bcheck.configure(command=check)
    bfolder.configure(command=open_folder)
    bfix.configure(command=fix_permissions)
    check()

    def poll():
        if not busy:
            check()
        root.after(2000, poll)
    root.after(2000, poll)
    if "--screenshot" in sys.argv:
        root.after(4000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
