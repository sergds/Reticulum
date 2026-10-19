# PICOBOOT USB Protocol in python for rnodeconf's rp2xxx flasher
# Copyright (c) 2026, Sergey Morozyuk <me@sergds.xyz>

import usb
import struct
import random
from enum import Enum
import atexit
import weakref
from collections.abc import Callable


class CommandError(RuntimeError):
    def __init__(self, errno: int):
        self.errno = errno
        self._msg = self._cmd_status_strerr(self.errno)

    def __str__(self):
        return self._msg

    def __repr__(self):
        return f"{self._msg} (error {self.errno})"

    def _cmd_status_strerr(self, code: int):
        match code:
            case 0:
                return "The command completed successfully (or is in still in progress)"
            case 1:
                return "The ID of the command was unrecognised"
            case 2:
                return "The length of the command request was incorrect"
            case 3:
                return "The data transfer length was incorrect given the command"
            case 4:
                return "The address specified was invalid for the command type"
            case 5:
                return "The address specified was incorrectly aligned"
            case 6:
                return "A Mass Storage Interface UF2 write has interfered with the current operation"
            case 7:
                return "The device is in the process of rebooting"
            case 8:
                return "Some non-specific error occurred"
            case 9:
                return "Invalid state"
            case 10:
                return "Permission violation"
            case 11:
                return "Argument is outside of range of supported values"
            case 12:
                return "The provided buffer was too small to hold the result"
            case 13:
                return "The operation failed because another bootrom function must be called first"
            case 14:
                return "Cached data was determined to be inconsistent"
            case 15:
                return "A data structure failed to validate"
            case 16:
                return "Attempted to access something that doesn’t exist; or a search failed"
            case 17:
                return "Unsupported modification"
            case _:
                return "Unknown command error"


class Command(Enum):
    EXCLUSIVE_ACCESS = 0x01
    REBOOT = 0x02
    FLASH_ERASE = 0x03
    READ = 0x84
    WRITE = 0x05
    EXIT_XIP = 0x06
    ENTER_XIP = 0x07
    EXEC = 0x08
    VECTORIZE_FLASH = 0x09
    REBOOT2 = 0x0a
    GET_INFO = 0x8b
    OTP_READ = 0x8c
    OTP_WRITE = 0x0d


class ExclusiveAccess(Enum):
    NOT_EXCLUSIVE = 0
    EXCLUSIVE = 1
    EXCLUSIVE_AND_EJECT = 2


class InfoType(Enum):
    INFO_SYS = 1
    PARTITION = 2
    UF2_TARGET_PARTITION = 3
    UF2_STATUS = 4


class SysInfoFlag(Enum):
    CHIP_INFO = 0x0001
    CRITICAL = 0x0002
    CPU_INFO = 0x0004
    FLASH_DEV_INFO = 0x0008
    BOOT_RANDOM = 0x0010
    NONCE = 0x0020
    BOOT_INFO = 0x0040


class DeviceType(Enum):
    RP2040 = 0
    RP2350 = 1


PICOBOOT_MAGIC = 0x431fd10b


def dprint_dummy(*args):
    print("\x1b[38;5;245m", "[PICOBOOT] ", *args, "\x1b[0m", sep='')
    return


def _rand32():
    return int.from_bytes(random.randbytes(4), "little")


class PBCommand():
    """PICOBOOT Command, contains magic, token, command's opcode, command's size, transfer lenght and up to 4 args words. Calling it returns serialized command"""
    def __init__(self, cmd: Command, cmd_size: int, xfer_len: int, args: bytes):
        self.magic = PICOBOOT_MAGIC
        self.token = _rand32()
        self.command = cmd.value
        self.size = cmd_size
        self.xfer_len = xfer_len
        self.b_args = args

    def _serialize_command(self):
        return struct.pack("<IIBBHI16s", self.magic, self.token, self.command, self.size, 0x0000, self.xfer_len, self.b_args)

    def __call__(self):
        return self._serialize_command()

    @staticmethod
    def exclusive_access(exclusive: int):
        return PBCommand(Command.EXCLUSIVE_ACCESS, 0x01, 0, struct.pack("<B", exclusive))

    @staticmethod
    def reboot(pc: int, sp: int, delay_ms: int):
        return PBCommand(Command.REBOOT, 0x0c, 0, struct.pack("<III", pc, sp, delay_ms))

    @staticmethod
    def flash_erase(addr: int, size: int):
        return PBCommand(Command.FLASH_ERASE, 0x08, 0, struct.pack("<II", addr, size))

    @staticmethod
    def read(addr: int, size: int):
        return PBCommand(Command.READ, 0x08, size, struct.pack("<II", addr, size))

    @staticmethod
    def write(addr: int, size: int):
        return PBCommand(Command.WRITE, 0x08, size, struct.pack("<II", addr, size))

    @staticmethod
    def exit_xip():
        return PBCommand(Command.EXIT_XIP, 0x00, 0, bytes([0]))

    @staticmethod
    def enter_xip():
        return PBCommand(Command.ENTER_XIP, 0x00, 0, bytes([0]))

    @staticmethod
    def exec(addr: int):
        return PBCommand(Command.EXEC, 0x04, 0, struct.pack("<I", addr))

    @staticmethod
    def vectorize_flash(addr: int):
        return PBCommand(Command.VECTORIZE_FLASH, 0x04, 0, struct.pack("<I", addr))

    @staticmethod
    def reboot2(flags: int, delay_ms: int, p0: int, p1: int):
        return PBCommand(Command.REBOOT2, 0x10, 0, struct.pack("<IIII", flags, delay_ms, p0, p1))

    @staticmethod
    def get_info(type: int, arg1: int, arg2: int, arg3: int):
        return PBCommand(Command.GET_INFO, 0x10, 64, struct.pack("<BBHIII", type, 0, 0, arg1, arg2, arg3))

    @staticmethod
    def otp_read(row: int, row_count: int, ecc: bool):
        return PBCommand(Command.OTP_READ, 0x05, row_count * (2 if ecc else 4), struct.pack("<IIB", row, row_count, int(ecc)))

    @staticmethod
    def otp_write(row: int, row_count: int, ecc: bool):
        return PBCommand(Command.OTP_WRITE, 0x05, row_count * (2 if ecc else 4), struct.pack("<IIB", row, row_count, int(ecc)))


class DebugLevel(Enum):
    ERR_WARN = 0
    INFO = 1
    TRACE = 2


class PBDevice():
    """PICOBOOT USB Device, found and opened by a usb bus-address. This is the main class for communicating with bootrom, has it's own micro logging system for some reason"""
    def __init__(self, bus: int, address: int, debug_print: Callable[...] = dprint_dummy, debug_level: DebugLevel = DebugLevel.TRACE):
        self.dprint = debug_print
        self.debug_level = debug_level.value
        self.active = True
        self._devtype = DeviceType.RP2040
        self._dev = usb.core.find(bus=bus, address=address)
        if self._dev is None:
            raise RuntimeError(f"failed to get device with serial \"{pserial_number}\"")
        self.picoboot_interface = None
        for intf in self._dev[0].interfaces():
            if intf.bInterfaceClass == 0xff:
                self.picoboot_interface = intf
                try:
                    self._dev.detach_kernel_driver(intf.bInterfaceNumber)
                except usb.core.USBError as e:
                    self.dprint_gated(DebugLevel.INFO, "detach_kernel_driver: ", e.__str__())
        if self.picoboot_interface == None:
            return RuntimeError("this device lacks PICOBOOT interface")
        #self._dev[0].set()
        usb.util.claim_interface(self._dev, self.picoboot_interface)
        self.ep_out = usb.util.find_descriptor(self.picoboot_interface, custom_match = lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in  = usb.util.find_descriptor(self.picoboot_interface, custom_match = lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        self.fin = weakref.finalize(self, self.close)
        self._devtype =  self._deduce_device_type()

    def dprint_gated(self, level: DebugLevel, *args):
        if level.value <= self.debug_level:
            self.dprint(*args)

    def _deduce_device_type(self):
        if self._dev.idProduct == 0x0003:
            return DeviceType.RP2040
        try:
            self.get_info(InfoType.INFO_SYS.value, [SysInfoFlag.CHIP_INFO.value])
            return DeviceType.RP2350
        except CommandError as e:
            if e.errno == 1:
                return DeviceType.RP2040

    def close(self):
        """Releases interface and destroys device handle"""
        self.active = False
        if self._dev:
            try:
                usb.util.release_interface(self._dev, self.picoboot_interface)
            except usb.core.USBError as e:
                self.dprint_gated(DebugLevel.ERR_WARN, e.strerror)
            self._dev, self.ep_in, self.ep_out = None, None, None
            self.dprint_gated(DebugLevel.INFO, "device closed.")

    def _rand32(self):
        return int.from_bytes(random.randbytes(4), "little")

    def _get_cmd_status(self):
        if not self.active or not self._dev:
            raise RuntimeError("the device is not open")
        status = self._dev.ctrl_transfer(usb.util.CTRL_TYPE_VENDOR | usb.util.ENDPOINT_IN | usb.util.CTRL_RECIPIENT_INTERFACE, 0x42, 0, self.picoboot_interface.index, 16, 1000)
        self.dprint_gated(DebugLevel.TRACE, "PICOBOOT CMD STATUS:", bytes(status).hex(" "))
        status_token, status_code, status_cmd, status_inprogress, status_reserved = struct.unpack("<IIBB6s", status)
        if len(status_reserved) != status_reserved.count(0):
            self.dprint_gated(DebugLevel.ERR_WARN, "FIXME: `reserved` in cmd status has changed")
        return {"status_token": status_token, "status_code": status_code, "status_cmd": status_cmd, "status_inprogress": status_inprogress, "status_reserved": status_reserved}

    def _reset_interface(self):
        self._dev.clear_halt(self.ep_in)
        self._dev.clear_halt(self.ep_out)
        r = self._dev.ctrl_transfer(usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_INTERFACE, 0x41, 0, self.picoboot_interface.index, 0, 1000)
        if r != 0:
            raise RuntimeError("failed to reset interface")

    def _do_command(self, cmd: PBCommand, txdata: bytes = None) -> bytes:
        xfer_len = cmd.xfer_len
        b_cmd = cmd()
        readdata = bytes()
        try:
            self.ep_out.write(b_cmd)
            self.dprint_gated(DebugLevel.TRACE, f"write({len(b_cmd)}): ", b_cmd.hex(' '))
            if xfer_len > 0:
                if cmd.command & 0x80:
                    readdata = self.ep_in.read(xfer_len, timeout=1000)
                    self.dprint_gated(DebugLevel.TRACE, f"read({len(readdata)}): ", bytes(readdata).hex(' '))
                else:
                    if txdata is None:
                        raise ValueError("txdata was not provided for a writing command")
                    self.ep_out.write(txdata)
                    self.dprint_gated(DebugLevel.TRACE, f"write({len(txdata)}): ", txdata.hex(' '))
            if cmd.command & 0x80:
                self.ep_out.write(bytes([]))
                self.dprint_gated(DebugLevel.TRACE, "send ACK")
            else:
                self.ep_in.read(1)
                self.dprint_gated(DebugLevel.TRACE, "recv ACK")
        except usb.core.USBError as e:
            if e.strerror == "Pipe error":
                status = self._get_cmd_status()
                self._reset_interface()
                raise CommandError(status["status_code"])
            elif e.strerror.startswith("No such device"):
                self.active = False
                self._dev = None
                raise RuntimeError("device was lost and had been forcibly closed")

        return readdata

    def exclusive_access(self, exclusive: int):
        self._do_command(PBCommand.exclusive_access(exclusive))

    def reboot(self, pc: int, sp: int, delay_ms: int):
        self._do_command(PBCommand.reboot(pc, sp, delay_ms))

    def flash_erase(self, addr: int, size: int):
        self._do_command(PBCommand.flash_erase(addr, size))

    def read(self, addr: int, size: int):
        Bresp = self._do_command(PBCommand.read(addr, size))
        return Bresp

    def write(self, addr: int, data: list[int]):
        self._do_command(PBCommand.write(addr, len(data)), bytes(data))

    def exit_xip(self):
        self._do_command(PBCommand.exit_xip())

    def enter_xip(self):
        self._do_command(PBCommand.enter_xip())

    def exec(self, addr: int):
        self._do_command(PBCommand.exec(addr))

    def vectorize_flash(self, addr: int):
        self._do_command(PBCommand.vectorize_flash(addr))

    def reboot2(self, flags: int, delay_ms: int, p0: int, p1):
        self._do_command(PBCommand.reboot2(flags, delay_ms, p0, p1))

    def get_info(self, type: int, args: list[int] = []):
        while len(args) < 3:
            args.append(0)
        res = self._do_command(PBCommand.get_info(type, args[0], args[1], args[2]))
        return res

    # don't need these for a flasher anyway?
    def otp_read(self, row: int, row_count: int, ecc: bool):
        raise RuntimeError("TODO: Unimplemented command")

    def otp_write(self, row: int, row_count: int, ecc: bool):
        raise RuntimeError("TODO: Unimplemented command")

    @staticmethod
    def enumerate_devices() -> list[usb.core.Device]:
        """Quick helper to list PICOBOOT devices"""
        return list(usb.core.find(find_all=True, custom_match = lambda d: d.idVendor==0x2e8a and (d.idProduct==0x000f or d.idProduct==0x0003)))

# test crap, if it doesn't error out, then it means you can probably flash with this module
if __name__ == "__main__":

    devs = PBDevice.enumerate_devices()

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

    chosen_dev = devs[chosen_n]
    pbdev = PBDevice(dev.bus, dev.address)
    atexit.register(pbdev.close)
    pbdev.write(0x20000000, [0] * 128)
    mem = pbdev.read(0x20000000, 128)
    assert list(mem) == [0] * 128
    pbdev.write(0x20000000, [0xFF, 0xFA, 0xFE, 0xFF] * 32)
    mem = pbdev.read(0x20000000, 128)
    pbdev.exit_xip()
    flash = pbdev.read(0x10000000, 128)
    print(bytes(flash).hex(' '))
    assert list(mem) == [0xFF, 0xFA, 0xFE, 0xFF] * 32
    try:
        pbdev.get_info(InfoType.INFO_SYS.value, [SysInfoFlag.CHIP_INFO.value])
    except CommandError as e:
        if pbdev._devtype != DeviceType.RP2040:
            raise e
    # pbdev.reboot2(0x0002, 800, 0, 0)
