#!/usr/bin/env python3
"""Python handheld overlays/scaling menu, matching Geometry and Grid."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Geometry_modeline'))
from geometry import Choice, Screen, Display, SessionCursor, draw_xbox360, install_termination_handlers, open_log
from grid_tool import display_lock
from overlays_overrides_backend import Engine, Plan

PREVIEWS = ('notice', 'menu', 'gamepad', 'keyboard', 'review', 'working', 'complete', 'recovery', 'error')
LABELS = {'install_all': 'Install overlays and scaling', 'install_scaling': 'Install scaling only',
          'uninstall': 'Uninstall / restore previous files'}


def notice():
    return Screen('BEFORE YOU START',
                  'Handheld scaling overrides, with optional borders.\n'
                  'For 15 kHz CRTs, including super resolutions.\n'
                  'Known profiles: NTSC, generic_15 and arcade_15.',
                  [Choice('Continue', 0), Choice('Return to EmulationStation', 0, 'quit')])


INSTALLED = ' - installed'
REPLACED = {'borders': 'Replaces the current overlays and scaling install.',
            'scaling': 'Replaces the current scaling-only install.',
            'legacy': 'Replaces the previous install.'}


def menu(selected=0, status=None):
    overlays = LABELS['install_all'] + (INSTALLED if status == 'borders' else '')
    scaling = 'Install scaling only (no borders)' + (INSTALLED if status == 'scaling' else '')
    choices = [Choice(overlays, 0, 'install_all'), Choice(scaling, 0, 'install_scaling')]
    if status: choices.append(Choice(LABELS['uninstall'], 0, 'uninstall'))  # Only offered when something is installed.
    choices += [Choice('Help - Button Mapping', 0, 'help'), Choice('Return to EmulationStation', 0, 'quit')]
    return Screen('OVERLAYS & SCALING', 'Handheld overrides for 15 kHz CRTs.', choices, selected=selected)


def help_screen(page='gamepad'):
    choices = ([Choice('Back to tool', 0, 'help_close'), Choice('Keyboard controls', 0, 'keyboard')]
               if page == 'gamepad' else
               [Choice('Back to gamepad controls', 0, 'gamepad'), Choice('Back to tool', 0, 'help_close')])
    return Screen('HELP - BUTTON MAPPING', '', choices, help_page=page)


def review(plan):
    if plan.action == 'uninstall':
        message = 'Restore saved originals; remove files this tool added.\nRestore the four Lynx / DS settings.'
        if plan.legacy: message = 'Legacy install: remove matching bundled files.\nRestore the saved Lynx / DS settings.'
        elif not plan.has_originals: message = 'No original backup: remove unchanged bundled files.\nRemove only the exact Lynx / DS values set by this tool.'
        if plan.skipped: message += '\nKeep ' + str(plan.skipped) + ' customized file(s).'
    else:
        count = plan.scaling + (plan.borders if plan.action == 'install_all' else 0)
        summary = str(count) + ' files; ' + str(plan.overwritten) + ' existing files replaced.'
        if plan.removed: summary += ' ' + str(plan.removed) + ' border files removed.'
        lines = [summary, 'Lynx: Handy / libretro. DS: top screen only.',
                 'Original files and settings are retained for uninstall.']
        if plan.replaces:
            return Screen('REPLACE INSTALLATION', '\n'.join([REPLACED[plan.replaces]] + lines),
                          [Choice('Cancel - keep current install', 0, 'cancel'),
                           Choice('Replace with ' + LABELS[plan.action], 0, 'apply')])
        message = '\n'.join(lines)
    return Screen('REVIEW CHANGES', message,
                  [Choice('Cancel - back to menu', 0, 'cancel'), Choice(LABELS[plan.action], 0, 'apply')])


def recovery_view(pending):
    completed = pending['phase'] == 'complete'
    return Screen('OPERATION RECOVERY',
                  ('The previous operation finished its verified writes.\nConfirm to finish its recovery record.' if completed else
                   'A previous operation was interrupted.\nRestore the files saved before it started.'),
                  [Choice('Return to EmulationStation', 0, 'quit'),
                   Choice('Finish verified operation' if completed else 'Restore previous files', 0, 'recover')])


def error_view():
    return Screen('OPERATION STOPPED',
                  'The operation could not complete.\nCheck recovery before retrying.\nDetails: overlays-overrides.log',
                  [Choice('Back to menu', 0, 'menu'), Choice('Return to EmulationStation', 0, 'quit')])


def preview(name):
    return {'notice': notice, 'menu': lambda: menu(status='borders'), 'gamepad': help_screen,
            'keyboard': lambda: help_screen('keyboard'),
            'review': lambda: review(Plan('install_all', [], {}, 50, 33, 4)),
            'working': lambda: Screen('APPLYING CHANGES', 'Installing files: 42 / 86\nPlease wait...', busy=True),
            'complete': lambda: Screen('COMPLETED', 'Installation completed and verified.',
                                     [Choice('Back to menu', 0, 'menu'), Choice('Return to EmulationStation', 0, 'quit')]),
            'recovery': lambda: recovery_view(dict(phase='applying')), 'error': error_view}[name]()


class OverlaysDisplay(Display):
    def __init__(self, pygame, window=False, size=None):
        super().__init__(pygame, window, size, tool_name='Overlays / Scaling',
                         subtitle='HANDHELD OVERRIDES & BORDERS', back_hint='B / ESC: back   HOTKEY+START: exit')

    def draw_help(self, view, preview=False):
        p, target = self.p, self.surface
        width, height = target.get_size(); scale = min(width / 640, height / 480)
        canvas = p.Surface((640, 480)); canvas.fill((5, 15, 5))
        green, dim, yellow = (0, 255, 102), (65, 177, 107), (255, 230, 0)
        p.draw.rect(canvas, (0, 0, 0), (18, 18, 604, 444)); p.draw.rect(canvas, green, (18, 18, 604, 444), 2)
        def text(value, x, y, size=16, color=green, center=False):
            image = p.font.SysFont('monospace', size, bold=True).render(value, True, color)
            canvas.blit(image, image.get_rect(midtop=(x, y)) if center else (x, y))
        text(view.title, 320, 34, 22, center=True)
        text('XBOX 360 POSITIONS' if view.help_page == 'gamepad' else 'KEYBOARD CONTROLS', 320, 65, 15, yellow, True)
        if view.help_page == 'gamepad':
            draw_xbox360(p, canvas, p.Rect(30, 106, 238, 180))
            controls = (('D-PAD/STICK', 'Choose in menus'), ('A (bottom)', 'Select / confirm'),
                        ('B (right)', 'Back / cancel'), ('BACK+START', 'Exit the tool'))
            for index, (control, action) in enumerate(controls):
                text(control, 282, 112 + index * 35, 14, yellow); text(action, 397, 112 + index * 35, 14)
            text('START alone: unused', 150, 292, 12, dim, True)
        else:
            controls = (('ARROWS / TAB', 'Choose a menu item'), ('ENTER / RETURN', 'Select / confirm'),
                        ('ESCAPE', 'Back / cancel'), ('CTRL + Q', 'Exit the tool'))
            for index, (control, action) in enumerate(controls):
                y = 103 + index * 42
                text(control, 36, y, 15, yellow); text(action, 36, y + 18, 13)
        for index, line in enumerate(('Changes require confirmation on the review screen.',
                                      'During application, exit waits for the operation to finish.')):
            text(line, 320, 319 + index * 17, 12, dim, True)
        for index, choice in enumerate(view.choices):
            rect = p.Rect(36, 363 + index * 31, 568, 28)
            chosen = index == view.selected
            if chosen: p.draw.rect(canvas, (0, 45, 18), rect, border_radius=4)
            text(('> ' if chosen else '  ') + choice.label, 45, rect.top + 5, 16, green if chosen else dim)
        text('D-PAD: choose   A: select   B: back   BACK+START: exit', 320, 433, 11, dim, True)
        if preview: text('PREVIEW ONLY', 320, 8, 10, yellow, True)
        size = (max(1, round(640 * scale)), max(1, round(480 * scale)))
        target.fill((5, 15, 5))
        target.blit(p.transform.smoothscale(canvas, size), ((width - size[0]) // 2, (height - size[1]) // 2))
        p.display.flip()


class Job:
    def __init__(self, operation):
        self.updates = queue.SimpleQueue(); self.error = None; self.result = ''
        def work():
            try: self.result = operation(lambda count, total: self.updates.put((count, total)))
            except BaseException as error: self.error = error
        self.thread = threading.Thread(target=work, name='overlays-write')
        self.thread.start()


def serve(display, engine, log, preview_name=None):
    p = display.p
    pending = None if preview_name else engine.pending()
    view = preview(preview_name) if preview_name else recovery_view(pending) if pending else notice()
    parent, plan, job, selected, failed, exit_after = None, None, None, 0, False, False
    armed = time.monotonic() + .3; clock = p.time.Clock()
    def main_menu(index):
        # Read the installed state each time the menu appears so its tags and Uninstall option stay current.
        try: return menu(index, None if engine is None else engine.status())
        except (OSError, ValueError, RuntimeError) as error:
            log.write('Install state unavailable: ' + str(error) + '\n'); log.flush()
            return error_view()
    try:
        while True:
            if job:
                while not job.updates.empty():
                    count, total = job.updates.get()
                    view.message = 'Updating files: ' + str(count) + ' / ' + str(total) + '\nPlease wait...'
                if not job.thread.is_alive():
                    job.thread.join()
                    if job.error:
                        failed = True; log.write('Operation stopped: ' + str(job.error) + '\n'); log.flush()
                        view = error_view()
                    else:
                        log.write(job.result + '\n'); log.flush()
                        if exit_after: return int(failed)
                        view = Screen('COMPLETED', job.result,
                                      [Choice('Back to menu', 0, 'menu'), Choice('Return to EmulationStation', 0, 'quit')])
                    job = None; armed = time.monotonic() + .3
            display.draw(view, preview_name is not None)
            for event in p.event.get():
                if event.type == p.QUIT:
                    if job: exit_after = True; continue
                    return int(failed)
                if event.type != p.KEYDOWN or time.monotonic() < armed: continue
                control_quit = bool(getattr(event, 'mod', 0) & p.KMOD_CTRL) and event.key in (p.K_q, p.K_ESCAPE)
                if job:
                    if control_quit or event.key == p.K_ESCAPE: exit_after = True
                    continue
                if control_quit: return int(failed)
                if not view.choices:
                    if event.key == p.K_ESCAPE: view = main_menu(selected)
                    continue
                if event.key in (p.K_UP, p.K_LEFT): view.selected = (view.selected - 1) % len(view.choices)
                elif event.key in (p.K_DOWN, p.K_RIGHT, p.K_TAB): view.selected = (view.selected + 1) % len(view.choices)
                elif event.key in (p.K_RETURN, p.K_KP_ENTER, p.K_SPACE, p.K_ESCAPE):
                    back = event.key == p.K_ESCAPE
                    action = 'cancel' if back else view.choices[view.selected].output
                    if view.help_page:
                        if action == 'keyboard': view = help_screen('keyboard')
                        elif action == 'gamepad' or back and view.help_page == 'keyboard': view = help_screen()
                        else: view, parent = parent or main_menu(selected), None
                    elif action == 'quit' or back and view.title in ('BEFORE YOU START', 'OVERLAYS & SCALING', 'OPERATION RECOVERY'):
                        return int(failed)
                    elif action == 'recover':
                        if preview_name: view = preview('complete')
                        else:
                            job = Job(lambda progress: engine.recover(pending['digest'], progress))
                            view = Screen('RECOVERING FILES', 'Please wait...', busy=True)
                    elif action == 'apply':
                        if preview_name: view = preview('complete')
                        else:
                            confirmed = plan
                            job = Job(lambda progress: engine.apply(confirmed, progress))
                            view = Screen('APPLYING CHANGES', 'Please wait...', busy=True)
                    elif action in LABELS:
                        selected = view.selected
                        try:
                            plan = Plan(action, [], {}, 50, 33) if preview_name else engine.plan(action)
                            view = review(plan)
                        except (OSError, ValueError, RuntimeError) as error:
                            failed = True; log.write('Review stopped: ' + str(error) + '\n'); log.flush(); view = error_view()
                    elif action == 'help': parent, view = view, help_screen()
                    elif view.title == 'BEFORE YOU START': view = main_menu(0)
                    else:
                        try:
                            pending = None if preview_name else engine.pending()
                            view = recovery_view(pending) if pending else main_menu(selected)
                        except (OSError, ValueError, RuntimeError) as error:
                            log.write('Recovery state unavailable: ' + str(error) + '\n'); log.flush(); view = error_view()
                    armed = time.monotonic() + .3
            clock.tick(40)
    finally:
        if job: job.thread.join()  # Do not abandon a transaction on a UI close/signal.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window', action='store_true'); parser.add_argument('--preview', choices=PREVIEWS)
    parser.add_argument('--render-preview', type=Path); parser.add_argument('--size', nargs=2, type=int)
    args = parser.parse_args(argv)
    if args.render_preview and not args.preview: parser.error('--render-preview requires --preview')
    if args.size and (not args.preview or not all(240 <= n <= 3840 for n in args.size)):
        parser.error('--size requires a preview and dimensions from 240 to 3840')
    os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
    if args.render_preview: os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
    pygame = None
    try:
        import pygame
        display = OverlaysDisplay(pygame, args.window or bool(args.render_preview), tuple(args.size) if args.size else None)
    except Exception as error:
        if pygame is not None: pygame.quit()
        print('Overlays interface unavailable: ' + str(error), file=sys.stderr); return 78
    cursor = None; previous = install_termination_handlers()
    try:
        if args.render_preview:
            display.draw(preview(args.preview), True)
            pygame.image.save(display.surface, str(args.render_preview)); return 0
        if args.preview: return serve(display, None, sys.stderr, args.preview)
        with open_log(Path('/userdata/system/logs/overlays-overrides.log')) as log:
            log.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' Python overlays/scaling tool started\n')
            cursor = SessionCursor(pygame.display.get_driver(), log, tool_name='Overlays / Scaling')
            engine = Engine()
            try:
                with display_lock(), engine.lock(): code = serve(display, engine, log)
                log.write('Overlays tool finished with status ' + str(code) + '\n'); log.flush()
                return code
            except (OSError, ValueError, RuntimeError) as error:
                log.write('Tool unavailable: ' + str(error) + '\n'); log.flush()
                view = Screen('TOOL UNAVAILABLE', 'Another tool may be active, or recovery needs attention.\nDetails: overlays-overrides.log',
                              [Choice('Return to EmulationStation', 0, 'quit')])
                clock = pygame.time.Clock(); armed = time.monotonic() + .3
                while True:
                    display.draw(view)
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT: return 1
                        if event.type == pygame.KEYDOWN and time.monotonic() >= armed and event.key in (pygame.K_RETURN, pygame.K_ESCAPE, pygame.K_q): return 1
                    clock.tick(40)
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
        print('Overlays tool stopped: ' + str(error), file=sys.stderr); return 1
    finally:
        try: pygame.quit()
        finally:
            if cursor: cursor.close()
            for number, handler in previous.items(): signal.signal(number, handler)


if __name__ == '__main__': raise SystemExit(main())
