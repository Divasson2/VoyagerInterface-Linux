# VoyagerInterface for Linux

Back up and restore the Continuous Memory (programs, registers, settings) of HP Voyager calculators over a normal USB cable. Python 3, Tkinter, and hidraw. Version 0.1.1.

Works with the HP 12C CE, 15C CE (15.0 and 15.2), and 16C CE.

## Disclaimer

VoyagerInterface is an **unofficial** tool. It is **not affiliated with, endorsed by, or supported by** HP or Moravia. “HP”, “12C”, “15C”, “16C”, and related names are trademarks of their respective owners and are used here only to describe which devices the tool works with.

The software is provided **“as is”, without warranty**. You use it **entirely at your own risk**. Writing a memory file **replaces the programs and data in your calculator**. A wrong or damaged file can clear the memory (“Pr Error”). The program always saves a backup first, but keep your own copies of anything important.

**This project ships no firmware** and never changes the firmware. It only reads and writes the 2 KB user memory (Continuous Memory). Prefer testing on a calculator whose memory you can afford to lose.

## What it does

A small Linux window that:

- **Read calculator to file** — saves the memory to `VoyagerInterface Backups` in your home folder (date and time in the name).
- **Write file to calculator** — first saves the calculator’s current memory as a backup, then writes the file and reads it back to check it.
- Warns you (but never blocks you) if the file seems to be from another model, or seems to need the HP-15C in 15.2 mode.

## Install

On Debian/Ubuntu-style systems, from this folder:

```sh
sh install.sh
```

The script installs `python3` and `python3-tk`, copies the program to `/opt/voyagerinterface`, installs a udev rule so your user can talk to the calculator (`plugdev` group), and adds a desktop launcher. It will ask for your password when it needs elevated privileges.

If it says **log out and log in once**, do that (or restart the machine) so the group membership applies. Then start **VoyagerInterface** from the desktop icon or the applications menu.

## Use

1. Connect the calculator with a normal USB cable.
2. Turn the calculator **off**. Hold **f** and **ENTER**, and press **ON**. The calculator is now in USB mode.
3. Open VoyagerInterface. The window should say **Calculator found**. If not, click **Check connection**.
4. **Back up:** click **Read calculator to file…**. The file lands in `VoyagerInterface Backups`. You can also save a copy somewhere else.
5. **Restore / load:** click **Write file to calculator…**, choose a 2,048-byte file (`.bin`, `.12c`, `.15c`, or `.16c`), and confirm. The program:
   1. reads the calculator and saves a backup (`calculator-before-write-….bin`),
   2. shows any warnings and asks “Write anyway?”,
   3. writes only the parts that changed, reads them back and checks them (up to 3 tries),
   4. says **Written and verified**.
6. Afterwards, turn the calculator off and on. To undo a write, write the `before-write` backup back.

HP-15C: the calculator and the file should use the same mode (15.0 or 15.2). To change the mode: turn it off, hold **g** and **ENTER**, press **ON**, then press **4** and choose. Changing the mode may clear the memory, so back up first.

If you use a virtual machine, make sure the USB device (VID:PID `03F0:1341`) is connected to Linux, not to the host.

## Fix permissions

If the window says the calculator was found but **Linux does not allow access**, click the **Fix permissions** button and type your password. That installs the same USB rule and `plugdev` access as `install.sh`. If it then asks you to log out and in, do that once.

You can also run `sh install.sh` again from this folder.

## Problems (short)

- **No calculator found**: do f-ENTER-ON again. The calculator leaves USB mode when it turns itself off. In a VM, check USB passthrough.
- **Linux does not allow access**: use **Fix permissions**, or run `sh install.sh`, then log out/in if asked.
- **The calculator stopped answering** or a failed check: put fresh batteries in, do f-ENTER-ON again, and retry. Your backup is in `VoyagerInterface Backups`.
- A log of USB traffic is written to `VoyagerInterface Backups/usb.log` if you need to report a problem.

## Licence

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Jose Gonzalez Divasson.

Run the tests with:

```sh
python3 tests/test_voyagerinterface.py
```
