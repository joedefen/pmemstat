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
import textwrap
from curses.textpad import rectangle, Textbox

class Window:
    """ Layer above curses to encapsulate what we need """
    timeout_ms = 200
    static_scr = None
    nav_keys = """
        Navigation:    H/M/L:   top/middle/end-of-page
            k, UP:  up one row               0, HOME:  first row
          j, DOWN:  down one row              $, END:  last row
           Ctrl-u:  half-page up       Ctrl-b, PPAGE:  page up
               Ctrl-d:  half-page down     Ctrl-f, NPAGE:  page down
    """
    def __init__(self, head_line=True, head_rows=10, body_rows=200,
                 body_cols=200, keys=None, pick_mode=False):
        self.scr = self._start_curses()
        self.head_rows = head_rows
        self.body_rows, self.body_cols = body_rows, body_cols
        self.head = curses.newpad(head_rows, body_cols)
        self.body = curses.newpad(body_rows, body_cols)
        self.head_row_cnt = 0  # no. head rows added
        self.head_view_cnt = 0  # no. head rows viewable
        self.hor_line_cnt = 1 if head_line else 0 # no. h-lines in header
        self.body_row_cnt = 0
        self.scroll_pos = 0  # how far down into body are we?
        self.pick_pos = 0 # in highlight mode, where are we?
        self.pick_mode = pick_mode # whether in highlight mode
        self.rows, self.cols = 0, 0
        self.scroll_view_size = 0  # no. viewable lines of the body
        self.max_scroll_pos = 0
        self.body_texts = []
        self.last_pick_pos = -1 # last highlighted position
        self.handled_keys = set(keys) if isinstance(keys, (set, list)) else []
        self._set_screen_dims()
        self.calc()
        
    @staticmethod
    def get_nav_keys_blurb():
        """For a help screen, describe the nav keys"""
        return textwrap.dedent(Window.nav_keys)

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

    def set_pick_mode(self, on=True):
        """Set whether in highlight mode."""
        was_on = self.pick_mode
        self.pick_mode = bool(on)
        if self.pick_mode and not was_on:
            self.last_pick_pos = -2 # indicates need to clear them all

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
        self.head_view_cnt = min(self.rows - self.hor_line_cnt, self.head_row_cnt)
        self.scroll_view_size = self.rows - self.head_view_cnt - self.hor_line_cnt
        self.max_scroll_pos = max(self.body_row_cnt - self.scroll_view_size, 0)
        self.body_base = self.head_view_cnt + self.hor_line_cnt
        return not same

    def add_header(self, text, attr=None, resume=False):
        """Add text to header"""
        if self.head_row_cnt < self.head_rows:
            if attr is None:
                attr = curses.A_NORMAL
            if resume:
                self.head.addstr(text, attr)
            else:
                self.head.addstr(self.head_row_cnt, 0, text, attr)
                self.head_row_cnt += 1

    def add_body(self, text, attr=None, resume=False):
        """ Add text to body (below header and header line)"""
        if self.body_row_cnt < self.body_rows:
            row = max(self.body_row_cnt - (1 if resume else 0), 0)
            if self.pick_mode or attr is None:
                attr = curses.A_NORMAL

            if resume:
                self.body.addstr(text, attr)
                self.body_texts[row] += text
            else:
                self.body.addstr(row, 0, text, attr)
                self.body_texts.append(text)
                self.body_row_cnt += 1
            
    def highlight_picked(self):
        if not self.pick_mode:
            return
        pos0, pos1 = self.last_pick_pos, self.pick_pos
        if pos0 == -2: # special flag to clear all formatting
            for row in range(self.body_row_cnt):
                self.body.addstr(row, 0, self.body_texts[row], curses.A_NORMAL)
        if pos0 != pos1:
            if 0 <= pos0 < self.body_row_cnt:
                self.body.addstr(pos0, 0, self.body_texts[pos0], curses.A_NORMAL)
            if 0 <= pos1 < self.body_row_cnt:
                self.body.addstr(pos1, 0, self.body_texts[pos1], curses.A_REVERSE)
                self.last_pick_pos = pos1

    def _scroll_indicator_row(self):
        """ Compute the absolute scroll indicator row:
        - We want the top to be only when scroll_pos==0
        - We want the bottom to be only when scroll_pos=max_scroll_pos-1
        """
        if self.max_scroll_pos <= 1:
            return self.body_base
        y2, y1 = self.scroll_view_size-1, 1
        x2, x1 = self.max_scroll_pos, 1
        x = self.scroll_pos
        pos = y1 + (y2-y1)*(x-x1)/(x2-x1)
        return min(self.body_base + int(max(pos, 0)), self.rows-1)

    def _scroll_indicator_col(self):
        """ Compute the absolute scroll indicator col:
        - We want the left to be only when scroll_pos==0
        - We want the right to be only when scroll_pos=max_scroll_pos-1
        """
        if self.pick_mode:
            return self._calc_indicator(
                self.pick_pos, 0, self.body_row_cnt-1, 0, self.cols-1)
        else:
            return self._calc_indicator(
                self.scroll_pos, 0, self.max_scroll_pos, 0, self.cols-1)
    
    @staticmethod
    def _calc_indicator(pos, pos0, pos9, ind0, ind9):
        if pos9 - pos0 <= 0:
            return -1 # not scrollable
        if pos <= pos0:
            return ind0
        if pos >= pos9:
            return ind9
        ind = int(round(ind0 + (ind9-ind0+1)*(pos-pos0)/(pos9-pos0+1)))
        return min(max(ind, ind0+1), ind9-1)

    def render(self):
        """Draw everything added."""
        self.calc()
        # if self.scroll_view_size <= 0:
            # self.scr.refresh()
        indent = 0
        if self.body_base < self.rows:
            ind_pos = 0 if self.pick_mode else self._scroll_indicator_row()
            if self.pick_mode:
                self.pick_pos = max(self.pick_pos, 0)
                self.pick_pos = min(self.pick_pos, self.body_row_cnt-1)
                if self.scroll_pos > self.pick_pos:
                    # light position is below body bottom
                    self.scroll_pos = self.pick_pos
                elif self.scroll_pos < self.pick_pos - (self.scroll_view_size - 1):
                    # light position is above body top
                    self.scroll_pos = self.pick_pos - (self.scroll_view_size - 1)
                indent = 1
            else:
                self.scroll_pos = max(self.scroll_pos, 0)
                self.scroll_pos = min(self.scroll_pos, self.max_scroll_pos)
                self.pick_pos = self.scroll_pos + ind_pos - self.body_base
                # indent = 1 if self.body_row_cnt > self.scroll_view_size else 0

        if indent > 0:
            if self.pick_mode:
                self.scr.vline(self.body_base, 0, ' ', self.scroll_view_size)
                pos = self.pick_pos - self.scroll_pos + self.body_base
                self.scr.addstr(pos, 0, '>', curses.A_REVERSE)
            else:
                self.scr.vline(self.body_base, 0, curses.ACS_VLINE, self.scroll_view_size)
                self.scr.addstr(ind_pos, 0, '|', curses.A_REVERSE)

        if self.head_view_cnt < self.rows:
            self.scr.hline(self.head_view_cnt, 0, curses.ACS_HLINE, self.cols)
            ind_pos = self._scroll_indicator_col()
            if ind_pos >= 0:
                self.scr.addstr(self.head_view_cnt, ind_pos, '-', curses.A_REVERSE)

        self.scr.refresh()

        if self.rows > 0:
            self.head.refresh(0, 0, 0, indent,
                      min(self.head_view_cnt, self.rows)-1, self.cols-1)

        if self.body_base < self.rows:
            if self.pick_mode:
                self.highlight_picked()
            self.body.refresh(self.scroll_pos, 0,
                  self.body_base, indent, self.rows-1, self.cols-1)


    def answer(self, prompt='Type string [then Enter]', seed='', width=80):
        """Popup"""
        def mod_key(key):
            return  7 if key == 10 else key

        # need 3 extra cols for rectangle (so we don't draw in southeast corner)
        # and 3 rows (top/prompt/bottom)
        #      +Prompt---    -----------------+
        #      | Seed-for-answer              |
        #      +---------Press ENTER to submit+
        assert self.rows >= 3 and self.cols >= 30, "window too small for prompt"
        width = min(width, self.cols-3) # max text width
        row0, row9 = self.rows//2 - 1, self.rows//2 + 1
        col0 = (self.cols - (width+2)) // 2
        col9 = col0 + width + 2 - 1

        self.scr.clear()
        win = curses.newwin(1, width, row0+1, col0+1) # input window
        rectangle(self.scr, row0, col0, row9, col9)
        self.scr.addstr(row0, col0+1, prompt[0:width])
        win.addstr(seed[0:width-1])
        ending = 'Press ENTER to submit'[:width]
        self.scr.addstr(row9, col0+1+width-len(ending), ending)
        self.scr.refresh()
        curses.curs_set(2)
        answer = Textbox(win).edit(mod_key).strip()
        curses.curs_set(0)
        return answer

    def alert(self, title='ALERT', message='', height=1, width=80):
        """Alert box"""
        def mod_key(key):
            return  7 if key == 10 else key

        # need 3 extra cols for rectangle (so we don't draw in southeast corner)
        # and 3 rows (top/prompt/bottom)
        #      +Prompt---    -----------------+
        #      | First line for message...    |
        #      | Last line for message.       |
        #      +-----------Press ENTER  to ack+
        assert self.rows >= 2+height and self.cols >= 30, "window too small for prompt"
        width = min(width, self.cols-3) # max text width
        row0 = (self.rows+height-1)//2 - 1
        row9 = row0 + height + 1
        col0 = (self.cols - (width+2)) // 2
        col9 = col0 + width + 2 - 1

        self.scr.clear()
        for row in range(self.rows):
            self.scr.insstr(row, 0, ' '*self.cols, curses.A_REVERSE)
        pad = curses.newpad(20, 200)
        win = curses.newwin(1, 1, row9-1, col9-2) # input window
        rectangle(self.scr, row0, col0, row9, col9)
        self.scr.addstr(row0, col0+1, title[0:width], curses.A_REVERSE)
        pad.addstr(message)
        ending = 'Press ENTER to ack'[:width]
        self.scr.addstr(row9, col0+1+width-len(ending), ending)
        self.scr.refresh()
        pad.refresh(0, 0, row0+1, col0+1, row9-1, col9-1)
        answer = Textbox(win).edit(mod_key).strip()
        return answer

    def clear(self):
        """Clear in prep for new screen"""
        self.scr.clear()
        self.head.clear()
        self.body.clear()
        self.body_texts, self.last_pick_pos = [], -1
        self.head_row_cnt = self.body_row_cnt = 0

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
            pos = self.pick_pos if self.pick_mode else self.scroll_pos
            was_pos = pos
            if key in (ord('k'), curses.KEY_UP):
                pos -= 1
            elif key in (ord('j'), curses.KEY_DOWN):
                pos += 1
            elif key in (ctl_b, curses.KEY_PPAGE):
                pos -= self.scroll_view_size
            elif key in (ctl_u, ):
                pos -= self.scroll_view_size//2
            elif key in (ctl_f, curses.KEY_NPAGE):
                pos += self.scroll_view_size
            elif key in (ctl_d, ):
                pos += self.scroll_view_size//2
            elif key in (ord('0'), curses.KEY_HOME):
                pos = 0
            elif key in (ord('$'), curses.KEY_END):
                pos = self.body_row_cnt - 1
            elif key in (ord('H'), ):
                pos = self.scroll_pos
            elif key in (ord('M'), ):
                pos = self.scroll_pos + self.scroll_view_size//2
            elif key in (ord('L'), ):
                pos = self.scroll_pos + self.scroll_view_size-1
                
            if self.pick_mode:
                self.pick_pos = pos
            else:
                self.scroll_pos = pos
                self.pick_pos = pos

            if pos != was_pos:
                self.render()
            # ignore unhandled keys
        return None

if __name__ == '__main__':
    def main():
        """Test program"""
        def do_key(key):
            nonlocal win, name, highlight
            answer = None
            if key == ord('q'):
                sys.exit(0)
            if key == ord('h'):
                highlight = not highlight
                win.set_highlight(on=highlight)
            if key == ord('n'):
                answer = win.answer(prompt='Provide Your Name:', seed='' if name.startswith('[hit') else name)
                win.alert(title='Info', message=f'got: {answer}')
            return answer

        keys_we_handle = [ord('q'), ord('n'), ord('h')]
        highlight = False

        win = Window(head_line=True, keys=keys_we_handle)
        body_size = win.scroll_view_size*4
        name = "[hit 'n' to enter name]"
        for loop in range(100000000000):
            win.add_header(f'Header: {loop} "{name}"')
            for line in range(body_size):
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
