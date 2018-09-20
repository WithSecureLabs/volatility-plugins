# Volatility
# Copyright (C) 2007-2013 Volatility Foundation
# Copyright (c) 2010, 2011, 2012 Michael Ligh <michael.ligh@mnin.org>
#
# This file is part of Volatility.
#
# Volatility is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Volatility is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Volatility.  If not, see <http://www.gnu.org/licenses/>.
#

import volatility.utils as utils
import volatility.obj as obj
import volatility.plugins.common as common
import volatility.debug as debug
import volatility.win32.tasks as tasks
import volatility.win32.modules as modules
import volatility.plugins.patchguard as patchguard
import volatility.plugins.overlays.windows.win8_kdbg as win8_kdbg
from volatility.renderers import TreeGrid
from volatility.renderers.basic import Address
import struct

#--------------------------------------------------------------------------------
# vtypes
#--------------------------------------------------------------------------------

timer_types_64 = {
    '_KTIMER_TABLE_ENTRY' : [ 0x18, {
    'Entry' : [ 0x0, ['_LIST_ENTRY']],
    'Time'  : [ 0x10, ['_ULARGE_INTEGER']],
    }]}
timer_types_32 = {
    '_KTIMER_TABLE_ENTRY' : [ 0x10, {
    'Entry' : [ 0x0, ['_LIST_ENTRY']],
    'Time'  : [ 0x8, ['_ULARGE_INTEGER']],
    }]}


class _KTIMER(obj.CType):
    
    @property
    def Dpc(self):

        vm = self.obj_vm
        profile = vm.profile
        bits = profile.metadata.get("memory_model")

        if bits == "32bit":
            return self.m("Dpc")

        # cycle through the parents until we reach the top 
        parent = self.obj_parent
        while parent and parent.obj_name != "_KDDEBUGGER_DATA64":
            parent = parent.obj_parent 
        
        if not parent:
            return obj.NoneObject("Parent is not a KDBG structure")

        # test if the patchguard magic is already available to us 
        if (not hasattr(parent, 'wait_always') or 
                not hasattr(parent, 'wait_never')):

            # this scans for the patchguard magic by indirectly 
            # finding the KdCopyDataBlock function  
            kdbg = win8_kdbg.VolatilityKDBG("", offset = 0,  vm = vm).v()
            if not kdbg:
                return obj.NoneObject("Cannot find KDBG structure")

            # transfer the attributes to our parent 
            parent.newattr('wait_never', kdbg.wait_never)
            parent.newattr('wait_always', kdbg.wait_always)  

        dpc = self.m("Dpc").v()

        decoded = patchguard.bswap(patchguard.rol(dpc ^ \
                    parent.wait_never, parent.wait_never & 0xFF) ^ \
                    self.obj_offset) ^ parent.wait_always

        return obj.Object("_KDPC", offset = decoded, vm = vm)

#--------------------------------------------------------------------------------
# profile modifications 
#--------------------------------------------------------------------------------

class TimerVTypes(obj.ProfileModification):
    before = ['WindowsOverlay']
    conditions = {'os': lambda x: x == 'windows'}
    def modification(self, profile):
        version = (profile.metadata.get('major', 0),
                   profile.metadata.get('minor', 0))
        if version < (6, 1):
            if profile.metadata.get("memory_model", "32bit") == "32bit":
                profile.vtypes.update(timer_types_32)
            else:
                profile.vtypes.update(timer_types_64)
        profile.object_classes.update({'_KTIMER': _KTIMER})

#--------------------------------------------------------------------------------
# timers
#--------------------------------------------------------------------------------

class Timers(common.AbstractWindowsCommand):
    """Print kernel timers and associated module DPCs"""

    def __init__(self, config, *args, **kwargs):
        common.AbstractWindowsCommand.__init__(self, config, *args, **kwargs)

        config.add_option('ListHead', short_option = 'L', default = None,
                      help = 'Virtual address of nt!KiTimerTableListHead',
                      action = 'store', type = 'int')

    def find_list_head(self, nt_mod, func, sig):
        """
        Find the KiTimerTableListHead given an exported
        function as a starting point and a small signature.

        @param nt_mod: _LDR_DATA_TABLE_ENTRY object for NT module
        @param func: function name exported by the NT module
        @param sig: byte string/pattern to use for finding the symbol
        """

        # Lookup the exported function 
        func_rva = nt_mod.getprocaddress(func)
        if func_rva == None:
            return None

        func_addr = func_rva + nt_mod.DllBase

        # Read enough of the function prolog 
        data = nt_mod.obj_vm.zread(func_addr, 300)

        # Scan for the byte signature 
        n = data.find(sig)
        if n == -1:
            return None

        return obj.Object('address', func_addr + n + len(sig), nt_mod.obj_vm)

    def find_list_head_offset(self, nt_mod, func, sig):
        # Lookup the exported function 
        func_rva = nt_mod.getprocaddress(func)
        if func_rva == None:
            return None

        func_addr = func_rva + nt_mod.DllBase

        # Read enough of the function prolog 
        data = nt_mod.obj_vm.zread(func_addr, 300)

        # Scan for the byte signature 
        n = data.find(sig)
        if n == -1:
            return None

        ptr = nt_mod.obj_vm.zread( func_addr + n+ len(sig), 4 )
        ptr = struct.unpack("I", ptr)[0]
        
        return ptr + func_addr + n + len(sig) + 4

    def calculate(self):
        addr_space = utils.load_as(self._config)

        # Get the OS version we're analyzing 
        version = (addr_space.profile.metadata.get('major', 0),
                   addr_space.profile.metadata.get('minor', 0))

        modlist = list(modules.lsmod(addr_space))
        mods = dict((addr_space.address_mask(mod.DllBase), mod) for mod in modlist)
        mod_addrs = sorted(mods.keys())

        # KTIMERs collected 
        timers = []

        # Valid KTIMER.Header.Type values 
        TimerNotificationObject = 8
        TimerSynchronizationObject = 9
        valid_types = (TimerNotificationObject, TimerSynchronizationObject)

        if version == (5, 1) or (version == (5, 2) and
                                addr_space.profile.metadata.get('build', 0) == 3789):

            # On XP SP0-SP3 x86 and Windows 2003 SP0, KiTimerTableListHead
            # is an array of 256 _LIST_ENTRY for _KTIMERs.

            if self._config.LISTHEAD:
                KiTimerTableListHead = self._config.LISTHEAD
            else:
                KiTimerTableListHead = self.find_list_head(modlist[0],
                                            "KeUpdateSystemTime",
                                            "\x25\xFF\x00\x00\x00\x8D\x0C\xC5")

            if not KiTimerTableListHead:
                debug.warning("Cannot find KiTimerTableListHead")
            else:
                lists = obj.Object("Array", offset = KiTimerTableListHead,
                                            vm = addr_space,
                                            targetType = '_LIST_ENTRY',
                                            count = 256)

                for l in lists:
                    for t in l.list_of_type("_KTIMER", "TimerListEntry"):
                        timers.append(t)

        elif version == (5, 2) or version == (6, 0):

            # On XP x64, Windows 2003 SP1-SP2, and Vista SP0-SP2, KiTimerTableListHead
            # is an array of 512 _KTIMER_TABLE_ENTRY structs.

            if self._config.LISTHEAD:
                KiTimerTableListHead = self._config.LISTHEAD
            else:
                if addr_space.profile.metadata.get("memory_model") == "32bit":
                    sigData = [ (False, "KeCancelTimer", "\xC1\xE7\x04\x81\xC7"), 
                                (True,  "KeUpdateSystemTime", "\x48\xB9\x00\x00\x00\x00\x80\xF7\xFF\xFF\x4C\x8D\x1D") ]
                else:
                    sigData = [ (True,  "KeCancelTimer", "\x48\x8D\x4C\x6D\x00\x48\x8D\x05"),  # lea rcx, [rbp+rbp*2+0] / lea rax, KiTimerTableListHead (nt.dll md5 825926D6AD714A529F4069D9EBBD1D3B)
                                (True,  "KeUpdateSystemTime", "\x48\xB9\x00\x00\x00\x00\x80\xF7\xFF\xFF\x4C\x8D\x1D")   # mov     rcx, 0FFFFF78000000000h / lea r11, KiTimerTableListHead (xp64sp1/2k3sp2 B1E08186348ED662D50118618F012445)
                              ]
                for sig in sigData:
                    if sig[0]:
                        KiTimerTableListHead = self.find_list_head_offset(modlist[0], sig[1], sig[2])
                    else:
                        KiTimerTableListHead = self.find_list_head(modlist[0], sig[1], sig[2])
                    if KiTimerTableListHead:
                        break

            if not KiTimerTableListHead:
                debug.warning("Cannot find KiTimerTableListHead")
            else:
                lists = obj.Object("Array", offset = KiTimerTableListHead,
                                            vm = addr_space,
                                            targetType = '_KTIMER_TABLE_ENTRY',
                                            count = 512)

                for l in lists:
#                    print "List at %s" % hex(int(l.obj_offset))
                    for t in l.Entry.list_of_type("_KTIMER", "TimerListEntry"):
#                        print "Timer at %s" % hex(int(t.obj_offset))
                        timers.append(t)

        elif version >= (6, 1):

            # Starting with Windows 7, there is no more KiTimerTableListHead. The list is
            # at _KPCR.PrcbData.TimerTable.TimerEntries (credits to Matt Suiche
            # for this one. See http://pastebin.com/FiRsGW3f).
            for kpcr in tasks.get_kdbg(addr_space).kpcrs():
                for table in kpcr.ProcessorBlock.TimerTable.TimerEntries:
                    for t in table.Entry.list_of_type("_KTIMER", "TimerListEntry"):
                        timers.append(t)

        for timer in timers:
            # Sanity check on the timer type 
            if timer.Header.Type not in valid_types:
#                print 'bad type %d' % timer.Header.Type
                continue

            # Ignore timers without DPCs
#            if not timer.Dpc.is_valid():
#                print 'dpc not valid'
#                continue
#            if not timer.Dpc.DeferredRoutine.is_valid():
#                print 'dpc deferredroutine not valid'
#                continue

            # Lookup the module containing the DPC
            module = tasks.find_module(mods, mod_addrs, addr_space.address_mask(timer.Dpc.DeferredRoutine))

            yield timer, module

    def unified_output(self, data):
        return TreeGrid([("Offset(V)", Address),
                       ("DueTime", str),
                       ("Period(ms)", int),
                       ("Signaled", str),
                       ("Routine", Address),
                       ("Module", str)],
                        self.generator(data))

    def generator(self, data):
        for timer, module in data:

            if timer.Header.SignalState.v():
                signaled = "Yes"
            else:
                signaled = "-"

            if module:
                module_name = str(module.BaseDllName or '')
            else:
                module_name = "UNKNOWN"

            due_time = "{0:#010x}:{1:#010x}".format(timer.DueTime.HighPart, timer.DueTime.LowPart)

            yield (0, [Address(timer.obj_offset), due_time, int(timer.Period), signaled, Address(timer.Dpc.DeferredRoutine), module_name])

    def render_text(self, outfd, data):

        self.table_header(outfd,
                        [("Offset(V)", "[addrpad]"),
                         ("DueTime", "24"),
                         ("Period(ms)", "10"),
                         ("Signaled", "10"),
                         ("Routine", "[addrpad]"),
                         ("Module", ""),
                        ])

        for timer, module in data:

            if timer.Header.SignalState.v():
                signaled = "Yes"
            else:
                signaled = "-"

            if module:
                module_name = str(module.BaseDllName or '')
            else:
                module_name = "UNKNOWN"

            due_time = "{0:#010x}:{1:#010x}".format(timer.DueTime.HighPart, timer.DueTime.LowPart)

            self.table_row(outfd,
                        timer.obj_offset,
                        due_time,
                        timer.Period,
                        signaled,
                        timer.Dpc.DeferredRoutine,
                        module_name)

