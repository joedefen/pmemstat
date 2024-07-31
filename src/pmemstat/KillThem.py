#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module to kill a set of processes and ensure they are really gone.
"""
# pylint: disable=broad-except,invalid-name,too-few-public-methods

import os
import sys
import signal
import time
import traceback

class KillThem():
    """Class to help kill a bunch of processes"""
    def __init__(self, pids):
        if isinstance(pids, (list, set, dict)):
            self.pids = set(list(pids))
        else:
            self.pids = set([pids])

    def do_kill(self):
        """Return True of all gone, else False"""
        sigs = [ signal.SIGTERM, signal.SIGTERM, signal.SIGTERM,
                 signal.SIGTERM, signal.SIGTERM, signal.SIGTERM,
                 signal.SIGTERM, signal.SIGTERM, signal.SIGTERM,
                 signal.SIGTERM, signal.SIGTERM, signal.SIGTERM,
                 signal.SIGKILL, signal.SIGKILL, signal.SIGKILL]
        last_sig = ''
        for sig in sigs:
            for pid in list(self.pids):
                try:
                    os.kill(int(pid), sig)
                    last_sig = sig
                except OSError:
                    self.pids.discard(pid)
            time.sleep(0.5)
            for pid in list(self.pids):
                # check if the process is still running
                if not os.path.exists(f"/proc/{pid}"):
                    self.pids.discard(pid)
            if not self.pids:
                break
        if self.pids:
            return False, f'Still running: {self.pids}'
        return True, f'Gone (w sig {last_sig})'

if __name__ == '__main__':
    def main():
        """Test stuff .. pass in one or more pids to kill"""
        killer = KillThem(sys.argv[1:])
        print(f'All gone: {killer.do_kill()}')
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exce:
        print("exception:", str(exce))
        print(traceback.format_exc())
