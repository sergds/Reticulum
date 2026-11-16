import picoboot
import struct

FLASH_BASE = 0x10000000

READ_BLOCK_SIZE = 128 * 1024


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

    # almost the same algorithm as here: https://github.com/raspberrypi/picotool/blob/2041936441b48a3cc53ae3da9e805229fe8f4e18/main.cpp#L3176
    def aprox_flash_size(self) -> int:
        self.pbdev.exit_xip()
        first_two_pages = self.pbdev.read(FLASH_BASE, 2 * 256)
        if len(set(first_two_pages)) <= 1:
            return 0  # all match
        min_size = 256 * 16
        max_size = 8 * 1024 * 1024
        size = max_size
        while size >= min_size:
            new_pages = self.pbdev.read(FLASH_BASE + size, 2 * 256)
            if new_pages != first_two_pages:
                break
            size >>= 1
        return size * 2


    def reboot_to_flash(self):
        if self.pbdev.get_device_type() == picoboot.DeviceType.RP2040:
            self.pbdev.reboot(0, 0, 1000)
        else:
            self.pbdev.reboot2(0x0004, 1000, FLASH_BASE, 0) # TODO: Specify actual start address, perhaps

    def read_flash(self, start: int = FLASH_BASE, end: int = -1) -> bytes:
        self.pbdev.exit_xip()
        real_start = start
        real_end = end
        if end == -1:
            real_end = FLASH_BASE + self.aprox_flash_size()

        start_offset = real_start % READ_BLOCK_SIZE
        end_residual = real_end % READ_BLOCK_SIZE

        for blockstart in range(real_start - start_offset, real_end, READ_BLOCK_SIZE):
            block = self.pbdev.read(blockstart, READ_BLOCK_SIZE)
            if blockstart // READ_BLOCK_SIZE == (real_start - start_offset) // READ_BLOCK_SIZE:
                if start_offset > 0:
                    block = block[start_offset:]
            if end_residual > 0 and (blockstart // READ_BLOCK_SIZE == real_end // READ_BLOCK_SIZE):
                block = block[:end_residual]
            yield bytes(block)

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
            yield page

    @staticmethod
    def context_from_file(path: str):
        with open(path, "rb") as f:
            fb = f.read()
            if (fb[0:4].decode('utf-8') == "UF2\n"):
                ctx = FlashContext()
                # uniq_familyids = {}
                for blockN in range(0, len(fb) // 512):
                    block_offs = blockN * 512
                    b_magic1, b_magic2, b_flags, b_addr, b_payload_size, b_num, b_numtotal, b_filesize_familyid, b_data, b_magicend = struct.unpack("<IIIIIIII476sI", fb[block_offs:block_offs+512])
                    if (b_magic1 != 0x0A324655 or b_magic2 != 0x9E5D5157):
                        raise RuntimeError("UF2 block has invalid initial magic numbers")
                    if (b_magicend != 0x0AB16F30):
                        raise RuntimeError("UF2 block ends with a wrong magic value")
                    if (b_flags & 1):
                        continue # block not for flashing
                    if (b_flags & 0x8000):
                        if (b_data[b_payload_size:b_payload_size+4] == bytes([0x04, 0xE3, 0x57, 0x99])):
                            continue # absolute block for RP2350-E10
                    ctx.pages.append(FlashPage(b_addr, b_data[:b_payload_size]))
                #     if (b_flags & 0x2000):
                #         if not b_filesize_familyid in uniq_familyids:
                #             uniq_familyids[b_filesize_familyid] = b_addr
                # print("UF2 families discovered:")
                # for fid, addr in uniq_familyids.items():
                #     print(hex(fid), "@", hex(addr))
                return ctx
            else:
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
    dry_run = False
    measure_flash = False
    save_flash = False

    print(f"PicoFlasher test  __main__ routine")

    def help():
        print(f"usage: {sys.argv[0]} [--measure-flash] [--dry-run] path/to/binary")
        print("\t--dry-run: load binary or uf2, but do not flash")
        print("\t--measure-flash: approximate flash size and exit")
        print("\t--save-flash: download flash into binary file (it will be trimmed!)")
        exit(1)

    if len(sys.argv) < 2:
        help()
    if "--dry-run" in sys.argv: dry_run = True
    if "--measure-flash" in sys.argv: measure_flash = True
    if "--save-flash" in sys.argv: save_flash = True

    positional = [narg for narg in sys.argv if not narg.startswith("-") and narg != sys.argv[0]]
    if len(positional) == 0 and not measure_flash:
        print("Please specify a binary")
        help()

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

    flasher = PicoFlasher(devs[chosen_n].bus, devs[chosen_n].address)

    if measure_flash:
        size = flasher.aprox_flash_size()
        print(size, hex(size))
        exit(0)

    if not save_flash:
        test_ctx = PicoFlasher.context_from_file(positional[0])
        print(test_ctx.pages)
        print(test_ctx)
    if dry_run: exit(0)

    if not save_flash:
        written_pages = 0
        for page_written in flasher.do_flash(test_ctx):
            print(f"\x1b[0KWrote @{page_written.addr:#04x} | {round(written_pages / len(test_ctx.pages), 4) * 100}% total", end="\r")
            written_pages += 1
        print()
        flasher.reboot_to_flash()
    else:
        read_pagesN = 0
        total_pages = flasher.aprox_flash_size() // READ_BLOCK_SIZE
        outf = open(positional[0], "wb")
        for page in flasher.read_flash():
            print(f"\x1b[0KReading flash | {round(read_pagesN / total_pages, 4) * 100}% total", end="\r")
            outf.write(page)
            read_pagesN += 1
        print()
        outf.close()
