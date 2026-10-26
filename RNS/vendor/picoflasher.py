import picoboot

FLASH_BASE = 0x10000000


class FlashPage():
    """Defines a block of flash memory. Specifies a block of data to write at some address"""
    def __init__(self, address: int, data: bytes):
        self.addr = address
        self.data = data
        if len(self.data) > 256:
            raise ValueError("page is larger than 256 bytes")
        if len(self.data) < 256:
            self.data = self.data + bytes([0]*(256-len(self.data)))
        assert len(self.data) == 256

    def __repr__(self):
        return f"<FlashPage @{self.addr:#04x}>"

    def __lt__(self, other: FlashPage):
        return self.addr < other.addr

    def __gt__(self, other: FlashPage):
        return self.addr > other.addr

    def __eq__(self, other: FlashPage):
        return self.addr == other.addr

    def __ne__(self, other: FlashPage):
        return self.addr != other.addr


class FlashContext():
    """Context contains data relevant to current flashing session. In particular it contains a set of `FlashPage`s to be flashed and tracks internal stuff like erased sectors"""
    def __init__(self, pages: list[FlashPage] = []):
        self.pages = pages
        self.erased_sectors: set[int] = set()
        # self.written_pages: set[int] = set()

    def __repr__(self):
        return f"<FlashContext with {len(self.pages)} pages>"


class PicoFlasher():
    def __init__(self, bus: int, address: int):
        self.pbdev = picoboot.PBDevice(bus, address)

    def reboot_to_flash(self):
        if self.pbdev.get_device_type() == picoboot.DeviceType.RP2040:
            self.pbdev.reboot(0, 0, 1000)
        else:
            self.pbdev.reboot2(0x0004, 1000, FLASH_BASE, 0) # TODO: Specify actual start address, perhaps

    def do_flash(self, ctx: FlashContext):
        self.pbdev.exit_xip()
        for page in ctx.pages:
            if page.addr % 256 != 0:
                raise RuntimeError("context's page address is not aligned to flash page")
            if not (page.addr // 4096) * 4096 in ctx.erased_sectors:
                sector = (page.addr // 4096) * 4096
                # self.pbdev.dprint_gated(picoboot.DebugLevel.TRACE, f"[FLASHER] erasing sector {sector:#04x}")
                self.pbdev.flash_erase(sector, 4096)
                ctx.erased_sectors.add(sector)
            # self.pbdev.dprint_gated(picoboot.DebugLevel.TRACE, f"[FLASHER] writing {page.addr:#04x}")
            self.pbdev.write(page.addr, page.data)

    @staticmethod
    def context_from_file(path: str):
        with open(path, "rb") as f:
            fb = f.read()
            # TODO: support UF2 and sense it here and branch automatically
            total_pages = len(fb) // 256 + (1 if len(fb) % 256 != 0 else 0)
            ctx = FlashContext()
            for p in range(total_pages):
                offs = 256 * p
                writing_address = FLASH_BASE + offs
                ctx.pages.append(FlashPage(writing_address, fb[offs:offs+256]))
            ctx.pages.sort()
            return ctx


if __name__ == "__main__":
    import sys
    print(f"PicoFlasher test  __main__ routine")
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} path/to/binary")
        exit(1)
    devs = picoboot.PBDevice.enumerate_devices()

    for devn in range(len(devs)):
        dev = devs[devn]
        print(f"{devn}. Device {dev.bus}-{dev.address}: {dev.product} ({dev.serial_number})")

    if len(devs) == 0:
        print("No PICOBOOT devices found!")
        exit(1)

    if len(devs) > 1:
        chosen_n = int(input("Choose device: "))
    else:
        chosen_n = 0

    test_ctx = PicoFlasher.context_from_file(sys.argv[1])
    print(test_ctx.pages)
    print(test_ctx)

    flasher = PicoFlasher(devs[chosen_n].bus, devs[chosen_n].address)
    flasher.do_flash(test_ctx)
    flasher.reboot_to_flash()
