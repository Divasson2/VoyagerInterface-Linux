# SPDX-License-Identifier: MIT
# Unit tests against a fake calculator behind a fake /dev/hidraw (a SOCK_SEQPACKET socket pair:
# one message per report, like hidraw). The fake behaves like the CE firmware: 0xFE dumps the
# image in reports of up to 63 bytes, 'C' blocks are copied in at once, and it "dies" (drops off
# USB) when a write changes its live CPU state 0x00..0x6F other than the flag-8 bits.
import os, socket, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import voyagerinterface as vi

HERE = os.path.dirname(__file__)


def sample(name):
    with open(os.path.join(HERE, "data", name), "rb") as f:
        return f.read()


class FakeCalculator(threading.Thread):
    def __init__(self, image, drop_block=None):
        super().__init__(daemon=True)
        self.mem = bytearray(image)
        self.host, self.dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.dead = False
        self.blocks = []
        self.drop_block = drop_block  # lose this block once
        self.start()

    def run(self):
        while True:
            try:
                m = self.dev.recv(100)
            except OSError:
                return
            if not m:
                return
            assert len(m) == 65 and m[0] == 0, "hidraw writes are report ID 0 + 64 bytes"
            r = m[1:]
            if self.dead:
                continue
            if r[0] == 0xFE:
                self.mem[0x7FB] = (self.mem[0x7FB] + 1) & 0xFF  # live counter
                pos = 0
                while pos < vi.MEMSIZE:
                    n = min(63, vi.MEMSIZE - pos)
                    rep = bytearray(64)
                    rep[0] = n
                    rep[1:1 + n] = self.mem[pos:pos + n]
                    self.dev.send(bytes(rep))
                    pos += n
            elif r[0] == 34 and r[1] == 0x43:
                b = r[2]
                if self.drop_block == b:
                    self.drop_block = None
                    continue
                self.blocks.append(b)
                new = bytearray(self.mem)
                new[b * 32:b * 32 + 32] = r[3:35]
                for i in range(0x70):
                    if new[i] != self.mem[i] and not (i == vi.FLAG8_OFF and (new[i] ^ self.mem[i]) == vi.FLAG8_MASK):
                        self.dead = True
                        self.dev.close()
                        return
                self.mem = new

    def link(self):
        return vi.Link(vi.HidrawTransport(fd=self.host.fileno()), read_timeout=2, command_pause=0, packet_pause=0)


class Tests(unittest.TestCase):
    def setUp(self):
        self.calc_img = sample("calc-15c-on.bin")       # read over USB, small memory
        self.big = sample("big-15c-synthetic.15c")      # 64 program registers (needs 15.2)
        self.off = bytearray(self.big)
        self.off[0x30] = 0x04                            # "saved while off", as an emulator file

    def test_framing(self):
        reps = vi.write_reports(bytes(range(256)) * 8)
        self.assertEqual(len(reps), 64)
        self.assertEqual(reps[5][:3], bytes([34, 0x43, 5]))
        self.assertEqual(reps[5][3:35], (bytes(range(256)) * 8)[160:192])
        self.assertEqual(len(reps[5]), 64)
        self.assertEqual(vi.dump_report()[0], 0xFE)

    def test_read(self):
        c = FakeCalculator(self.calc_img)
        d = c.link().read_memory()
        self.assertEqual(len(d), 2048)
        self.assertEqual(d[:0x7F0], self.calc_img[:0x7F0])

    def test_write_keeps_cpu_state_and_verifies(self):
        c = FakeCalculator(self.calc_img)
        back = c.link().write_memory(bytes(self.off), with_flag8=vi.flag8(self.off))
        self.assertFalse(c.dead)
        self.assertEqual(back[0x70:0x7F0], self.big[0x70:0x7F0])
        self.assertEqual(back[0x30], 0x0C)              # calculator's own power state kept
        self.assertNotIn(0, c.blocks); self.assertNotIn(1, c.blocks)
        self.assertEqual(back[0x7F0:0x7F8], self.calc_img[0x7F0:0x7F8])  # trailer kept

    def test_naive_write_kills_the_fake(self):
        # the 0.5.2 bug: sending every block including the "off" CPU state
        c = FakeCalculator(self.calc_img)
        t = vi.HidrawTransport(fd=c.host.fileno())
        with self.assertRaises(vi.LinkError):   # like the Mac log: a later block fails
            for r in vi.write_reports(bytes(self.off)):
                t.send(r)
                import time; time.sleep(0.005)
        self.assertTrue(c.dead)

    def test_flag8(self):
        img = bytearray(self.calc_img); img[vi.FLAG8_OFF] |= vi.FLAG8_MASK; img[vi.S0A_OFF] |= 1
        c = FakeCalculator(self.calc_img)
        back = c.link().write_memory(bytes(img), with_flag8=True)
        self.assertTrue(vi.flag8(back))
        self.assertEqual(back[vi.S0A_OFF] & 1, 1)
        m = vi.merge(self.calc_img, bytes(img), with_flag8=False)
        self.assertFalse(vi.flag8(m))
        self.assertEqual(m[vi.S0A_OFF] & 1, 0)
        self.assertEqual(m[:vi.FLAG8_OFF], img[:vi.FLAG8_OFF])

    def _complex_on_and_reset(self):
        on = bytearray(self.calc_img); on[vi.FLAG8_OFF] |= vi.FLAG8_MASK; on[vi.S0A_OFF] |= 1
        reset = bytearray(self.calc_img); reset[vi.FLAG8_OFF] &= ~vi.FLAG8_MASK & 0xFF
        reset[0x70:0x7F0] = bytes(0x780)                       # memory reset: user area cleared
        return bytes(on), bytes(reset)

    def test_bug_B_read_reset_write_keeps_flag8(self):
        # Jose's sequence: read (complex on) -> file (*.bin, as "Read" saves it) -> memory reset -> write
        on, reset = self._complex_on_and_reset()
        saved = FakeCalculator(on).link().read_memory()
        self.assertTrue(vi.flag8(saved))
        path = vi.backup_name("read")                          # calculator-read-....bin
        f8 = vi.flag8_for_write(path, saved)
        self.assertIs(f8, True)                                 # taken from the FILE
        c = FakeCalculator(reset)
        back = c.link().write_memory(saved, with_flag8=f8)
        self.assertIn(2, c.blocks)                              # block 0x40..0x5F sent for the bit
        self.assertTrue(vi.flag8(back))                         # verify saw it
        self.assertTrue(vi.flag8(bytes(c.mem)))
        self.assertEqual(back[0x30], reset[0x30])               # rest of CPU state is the calculator's

    def test_bug_B_cause_in_0_1_0(self):
        # 0.1.0 passed flag 8 only for *.15c names; a *.bin went with None -> calculator's 0 kept
        on, reset = self._complex_on_and_reset()
        c = FakeCalculator(reset)
        back = c.link().write_memory(on, with_flag8=None)
        self.assertFalse(vi.flag8(back))
        self.assertNotIn(2, c.blocks)

    def test_flag8_only_difference_sends_block2(self):
        on, _ = self._complex_on_and_reset()
        off = bytearray(on); off[vi.FLAG8_OFF] &= ~vi.FLAG8_MASK & 0xFF
        c = FakeCalculator(bytes(off))
        back = c.link().write_memory(on, with_flag8=True)
        self.assertIn(2, c.blocks); self.assertTrue(vi.flag8(back))

    def test_verify_catches_ignored_flag8(self):
        on, reset = self._complex_on_and_reset()
        c = FakeCalculator(reset)
        class DropBlock2(vi.HidrawTransport):
            def send(self, r):
                if r[0] == 34 and r[2] == 2:
                    return
                super().send(r)
        with self.assertRaises(vi.LinkError) as e:
            vi.Link(DropBlock2(fd=c.host.fileno()), read_timeout=2, command_pause=0, packet_pause=0).write_memory(on, with_flag8=True)
        self.assertIn("flag 8", str(e.exception))

    def test_flag8_for_write_names(self):
        img = bytearray(2048); img[vi.FLAG8_OFF] = vi.FLAG8_MASK
        self.assertIs(vi.flag8_for_write("a.bin", img), True)
        self.assertIs(vi.flag8_for_write("a.15c", img), True)
        self.assertIsNone(vi.flag8_for_write("a.16c", img))
        self.assertIsNone(vi.flag8_for_write("a.12c", img))

    def test_fix_script(self):
        s = vi.fix_script("divasson", "/dev/hidraw4")
        self.assertIn('GROUP="plugdev"', s); self.assertIn("usermod -aG plugdev divasson", s)
        self.assertIn("chgrp plugdev /dev/hidraw4", s); self.assertIn("udevadm control --reload-rules", s)

    def test_retry_after_dropped_block(self):
        c = FakeCalculator(self.calc_img, drop_block=50)
        back = c.link().write_memory(self.big)
        self.assertEqual(back[0x70:0x7F0], self.big[0x70:0x7F0])
        self.assertEqual(c.blocks.count(50), 1)

    def test_unchanged_write_sends_nothing(self):
        c = FakeCalculator(self.calc_img)
        c.link().write_memory(self.calc_img)
        self.assertEqual(c.blocks, [])

    def test_wrong_size(self):
        with self.assertRaises(vi.LinkError):
            vi.write_reports(b"\0" * 100)

    def test_timeout_when_silent(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with self.assertRaises(vi.LinkError):
            vi.Link(vi.HidrawTransport(fd=a.fileno()), read_timeout=0.3, command_pause=0).read_memory()
        a.close(); b.close()

    def test_warnings_never_block_and_flag_152(self):
        w = vi.warnings_for(self.big, self.calc_img, "large-synthetic.15c")
        self.assertTrue(any("15.2" in x for x in w))
        w = vi.warnings_for(self.calc_img, self.calc_img, "small.15c")
        self.assertFalse(any("Error 10" in x for x in w))
        w16 = sample("calc-16c-on.16c")
        w = vi.warnings_for(w16, self.calc_img, "x.16c")
        self.assertTrue(any("different calculator model" in x for x in w))

    def test_find_hidraw(self):
        d = tempfile.mkdtemp()
        for n, hid in (("hidraw0", "0003:0000046D:0000C52B"), ("hidraw3", "0003:000003F0:00001341")):
            os.makedirs(os.path.join(d, n, "device"))
            with open(os.path.join(d, n, "device", "uevent"), "w") as f:
                f.write("DRIVER=hid-generic\nHID_ID=%s\nHID_NAME=x\n" % hid)
        self.assertEqual(vi.find_hidraw(d), "/dev/hidraw3")
        self.assertIsNone(vi.find_hidraw(os.path.join(d, "none")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
