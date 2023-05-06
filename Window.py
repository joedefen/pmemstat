#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Custom Wrapper for python curses.
"""
# pylint: disable=too-many-instance-attributes,too-many-arguments
# pylint: disable=invalid-name

import atexit
import curses

class Window:
    """ Layer above curses to encapsulate what we need """
    timeout_ms = 200
    static_scr = None
    """TBD"""
    def __init__(self, head_line=True, head_rows=10, body_rows=200,
                 body_cols=200, keys=None):
        self.scr = self._start_curses()
        self.head_rows = head_rows
        self.body_rows, self.body_cols = body_rows, body_cols
        self.head = curses.newpad(head_rows, body_cols)
        self.body = curses.newpad(body_rows, body_cols)
        self.head_count = 0
        self.head_lines = 1 if head_line else 0
        self.body_count = 0
        self.scroll_pos = 0  # how far down into body are we?
        self.rows, self.cols = 0, 0
        self.max_body_count, self.max_scroll = 0, 0
        self.handled_keys = keys if isinstance(keys, set) else []
        self._set_screen_dims()
        self.calc()

    def _set_screen_dims(self):
        """Recalculate dimensions ... return True if geometry changed."""
        rows, cols = self.scr.getmaxyx()
        same = bool(rows == self.rows and cols == self.cols)
        self.rows, self.cols = rows, cols
        return same

    @staticmethod
    def _start_curses():
        """ Curses initial setup.  Note: not using curses.wrapper because we
        don't wish to change the colors. """
        atexit.register(Window.stop_curses)
        Window.static_scr = scr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        curses.curs_set(0)
        scr.keypad(1)
        scr.timeout(Window.timeout_ms)
        scr.clear()
        return scr

    @staticmethod
    def stop_curses():
        """ Curses shutdown (registered to be called on exit). """
        if Window.static_scr:
            curses.nocbreak()
            curses.echo()
            Window.static_scr.keypad(0)
            curses.endwin()
            Window.static_scr = None

    def calc(self):
        """Recalculate dimensions ... return True if geometry changed."""
        same = self._set_screen_dims()
        self.head_count = min(self.rows - self.head_lines, self.head_count)
        self.max_body_count = self.rows - self.head_count - self.head_lines
        self.max_scroll = max(self.body_count - self.max_body_count, 0)
        self.body_base = self.head_count + self.head_lines
        return not same

    def add_header(self, text, attr, resume=False):
        """Add text to header"""
        if self.head_count < self.head_rows:
            if attr:
                if resume:
                    self.head.addstr(text, attr)
                else:
                    self.head.addstr(self.head_count, 0, text, attr)
            else:
                if resume:
                    self.head.addstr(text)
                else:
                    self.head.addstr(self.head_count, 0, text)
            self.head_count += 1

    def add_body(self, text, attr=None):
        """ Add text to body (below header and header line)"""
        if self.body_count < self.body_rows:
            if attr:
                self.body.addstr(self.body_count, 0, text, attr)
            else:
                self.body.addstr(self.body_count, 0, text)
            self.body_count += 1

    def _scroll_indicator_row(self):
        """ Compute the absolute scroll indicator row:
        - We want the top to be only when scroll_pos==0
        - We want the bottom to be only when scroll_pos=max_scroll-1
        """
        if self.max_scroll <= 1:
            return self.body_base
        y2, y1 = self.max_body_count-1, 1
        x2, x1 = self.max_scroll, 1
        x = self.scroll_pos
        pos = y1 + (y2-y1)*(x-x1)/(x2-x1)
        return min(self.body_base + int(max(pos, 0)), self.rows-1)

    def render(self):
        """Draw everything added."""
        self.calc()
        if self.max_scroll <= 0:
            self.scr.refresh()
        if self.head_count < self.rows:
            # rectangle(self.scr, self.head_count, 0, self.head_count, self.cols-1)
            self.scr.hline(self.head_count, 0, curses.ACS_HLINE, self.cols)
        if self.max_scroll > 0:
            # rectangle(self.scr, self.body_base, 0, self.rows-1, 0)
            self.scr.vline(self.body_base, 0, curses.ACS_VLINE, self.max_body_count)
            self.scr.addstr(self._scroll_indicator_row(), 0, '|', curses.A_REVERSE)
            self.scr.attron(curses.A_REVERSE)
            # self.scr.vline(self._scroll_indicator_row(), 0, curses.ACS_VLINE, 1)
            self.scr.attroff(curses.A_REVERSE)
            self.scr.refresh()

        if self.rows > 0:
            self.head.refresh(0, 0, 0, 1 if self.max_scroll > 0 else 0,
                      min(self.head_count, self.rows)-1, self.cols-1)
        if self.body_base < self.rows:
            self.scroll_pos = max(self.scroll_pos, 0)
            self.scroll_pos = min(self.scroll_pos, self.max_scroll)
            self.body.refresh(self.scroll_pos, 0,
                  self.body_base, 1 if self.max_scroll > 0 else 0,
                  self.rows-1, self.cols-1)

    def clear(self):
        """Clear in prep for new screen"""
        self.scr.clear()
        self.head.clear()
        self.body.clear()
        self.head_count = self.body_count = 0

    def prompt(self, seconds=1.0):
        """Here is where we sleep waiting for commands or timeout"""
        ctl_b, ctl_d, ctl_f, ctl_u = 2, 4, 6, 21
        elapsed = 0.0
        while elapsed < seconds:
            key = self.scr.getch()
            if key == curses.ERR:
                elapsed += self.timeout_ms / 1000
                continue
            if key in (curses.KEY_RESIZE, ) or curses.is_term_resized(self.rows, self.cols):
                # self.scr.erase()
                self._set_screen_dims()
                # self.render()
                break

            # Navigation Keys...
            was_scroll_pos = self.scroll_pos
            if key in (ord('k'), curses.KEY_UP):
                self.scroll_pos = max(self.scroll_pos - 1, 0)
            elif key in (ord('j'), curses.KEY_DOWN):
                self.scroll_pos += 1
            elif key in (ctl_b, curses.KEY_PPAGE):
                self.scroll_pos -= self.max_body_count
            elif key in (ctl_u, ):
                self.scroll_pos -= self.max_body_count//2
            elif key in (ctl_f, curses.KEY_NPAGE):
                self.scroll_pos += self.max_body_count
            elif key in (ctl_d, ):
                self.scroll_pos += self.max_body_count//2
            elif key in (ord('H'), curses.KEY_HOME):
                self.scroll_pos = 0
            elif key in (ord('$'), curses.KEY_END):
                self.scroll_pos = self.body_count

            if self.scroll_pos != was_scroll_pos:
                self.render()
            # App keys...
            elif key in self.handled_keys:
                return key # return for handling
            # ignore unhandled keys
        return None
