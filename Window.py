#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Custom Wrapper for python curses.
"""
# pylint: disable=too-many-instance-attributes,too-many-arguments
# pylint: disable=invalid-name,broad-except

import sys
import traceback
import atexit
import curses
from curses.textpad import rectangle, Textbox

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
        self.popup = curses.newpad(4, body_cols)
        self.popup_cols = 0  # popup show when positive
        self.head_count = 0
        self.head_lines = 1 if head_line else 0
        self.body_count = 0
        self.scroll_pos = 0  # how far down into body are we?
        self.rows, self.cols = 0, 0
        self.max_body_count, self.max_scroll = 0, 0
        self.handled_keys = set(keys) if isinstance(keys, (set, list)) else []
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

    def add_header(self, text, attr=None, resume=False):
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
            self.head_count += 0 if resume else 1

    def add_body(self, text, attr=None, resume=False):
        """ Add text to body (below header and header line)"""
        if self.body_count < self.body_rows:
            if attr:
                if resume:
                    self.body.addstr(text, attr)
                else:
                    self.body.addstr(self.body_count, 0, text, attr)
            else:
                if resume:
                    self.body.addstr(text)
                else:
                    self.body.addstr(self.body_count, 0, text)
            self.body_count += 0 if resume else 1

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
        if self.popup_cols > 0 and self.rows >= 4:
            popup_left = max(0, (self.cols - self.popup_cols)//2)
            popup_right = min(self.cols-1, popup_left+self.popup_cols)
            self.popup.refresh(0, 0,
                               self.rows//2-2, popup_left,
                               self.rows//2+1, popup_right
                               )

    def answer(self, prompt='Type string [then Enter]', seed='', width=80):
        """Popup"""
        def mod_key(key):
            return  7 if key == 10 else key
        
        # need 3 extra cols for rectangle (so we don't draw in southeast corner)
        # and 3 extra rows (top/bottom/prompt)
        #      +---------+
        #      | Prompt  |
        #      | Answer  |
        #      +---------+
        assert self.rows >= 5 and self.cols >= 30, "window too small for prompt"
        width = min(width, self.cols-3) # max text width
        row0, row9 = self.rows//2 - 2, self.rows//2 + 1
        col0 = (self.cols - (width+2)) // 2
        col9 = col0 + width + 2 - 1
        
        self.scr.clear()
        win = curses.newwin(1, width, row0+2, col0+1) # input window
        rectangle(self.scr, row0, col0, row9, col9)
        self.scr.addstr(row0+1, col0+1, prompt[0:width], curses.A_REVERSE)
        win.addstr(seed[0:width-1])
        self.scr.refresh()
        curses.curs_set(2)
        answer = Textbox(win).edit(mod_key).strip()
        curses.curs_set(0)
        return answer

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

            # App keys...
            if key in self.handled_keys:
                return key # return for handling

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
            # ignore unhandled keys
        return None

if __name__ == '__main__':
    def main():
        """Test program"""
        def do_key(key):
            nonlocal win, name
            answer = None
            if key == ord('q'):
                sys.exit(0)
            if key == ord('n'):
                answer = win.answer(prompt='Type Name + Enter',
                    seed='' if name.startswith('[hit') else name)
            return answer

        keys_we_handle = [ord('q'), ord('n')]

        win = Window(head_line=True, keys=keys_we_handle)
        name = "[hit 'n' to enter name]"
        for loop in range(100000000000):
            win.add_header(f'Header: {loop} "{name}"')
            for line in range(win.max_body_count*2):
                win.add_body(f'Body: {loop}.{line}')
            win.render()
            answer = do_key(win.prompt(seconds=5))
            if answer is not None:
                name = answer
            win.clear()

    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exce:
        Window.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
