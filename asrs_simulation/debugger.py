"""Pygame inspection of the same analytical trajectories used by headless runs."""

from bisect import bisect_right
import os

os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
import pygame


BG = (17, 24, 36)
PANEL = (27, 38, 53)
TEXT = (226, 235, 244)
MUTED = (148, 168, 189)
GREEN = (71, 209, 166)
GOLD = (255, 197, 87)


class Renderer:
    def __init__(self, schedule, map_data, totes, title='ASRS'):
        pygame.init()
        self.schedule, self.map, self.totes = schedule, map_data, totes
        cfg = schedule.config['display']
        self.screen = pygame.display.set_mode((cfg['width'], cfg['height']))
        pygame.display.set_caption(f'ASRS debug — {title}')
        self.font = pygame.font.SysFont('DejaVu Sans', 16)
        self.small = pygame.font.SysFont('DejaVu Sans', 12)
        self.large = pygame.font.SysFont('DejaVu Sans', 24, bold=True)
        self.selected = 0
        self.names = list(schedule.cranes)
        self.width, self.height = self.screen.get_size()
        self.static = {}
        self.buttons = []

    def text(self, value, point, color=TEXT, font=None, surface=None):
        (surface or self.screen).blit((font or self.font).render(str(value), True, color), point)

    def overview(self, pos):
        grid = self.map['grid']
        return (int(40+pos[0]/grid['width_m']*(self.width*.62-60)),
                int(145+pos[1]/grid['length_m']*220))

    def elevation(self, pos, side):
        return (int(40+pos[0]/self.map['grid']['width_m']*(self.width-80)),
                int(self.height-240+side*145-pos[2]/self.map['storage_layout']['rack_height_m']*105))

    def background(self):
        if self.selected in self.static:
            return self.static[self.selected]
        bg = pygame.Surface(self.screen.get_size())
        bg.fill(BG)
        name = self.names[self.selected]
        selected = self.schedule.cranes[name]
        self.text('ASRS  /  tote retrieval & return', (25, 15), font=self.large, surface=bg)
        self.text('SPACE pause   +/- speed   N next event (paused)   1–9 / TAB select aisle   ESC close',
                  (25, 50), color=MUTED, font=self.small, surface=bg)
        self.buttons = []
        button_width = min(150, (self.width-50)//len(self.names))
        for index, crane_id in enumerate(self.names):
            rect = pygame.Rect(25+index*button_width, 78, button_width-8, 30)
            self.buttons.append(rect)
            pygame.draw.rect(bg, (40, 91, 98) if index == self.selected else PANEL, rect, border_radius=5)
            self.text(crane_id, (rect.x+10, rect.y+6), surface=bg)
        grid = self.map['grid']
        dx, dy = grid['spacing_m'], grid.get('spacing_y_m', grid['spacing_m'])
        for marker in self.map['markers']:
            point = self.overview((marker['column']*dx, marker['row']*dy, 0))
            if marker['role'] == 'rack':
                color = (67, 110, 130) if marker['row'] in selected.rows else (46, 64, 81)
                pygame.draw.rect(bg, color, (point[0]-6, point[1]-4, 12, 8))
            elif marker['role'] == 'workstation':
                pygame.draw.rect(bg, GOLD, (point[0]-6, point[1]-5, 12, 10))
        for crane in self.schedule.cranes.values():
            left, right = self.overview((0, crane.home[1], 0)), self.overview(crane.home)
            pygame.draw.line(bg, (56, 93, 109), left, right, 1)
        occupied = {(t.rack, t.level, t.slot) for t in self.totes}
        for side in (0, 1):
            bottom = self.height-240+side*145
            self.text(('LEFT' if side == 0 else 'RIGHT')+f' RACK  ·  row {selected.rows[side]}',
                      (25, bottom-132), color=MUTED, font=self.small, surface=bg)
            for slot in self.map['storage_layout']['slots']:
                if int(slot['rack_id'].split('_')[1]) != selected.rows[side]:
                    continue
                point = self.elevation(tuple(slot['center_'+a] for a in 'xyz'), side)
                key = (slot['rack_id'], slot['level'], slot['slot'])
                color = (57, 93, 112) if key in occupied else (30, 46, 62)
                pygame.draw.rect(bg, color, (point[0]-5, point[1]-9, 10, 18), border_radius=2)
            pygame.draw.line(bg, MUTED, (35, bottom+7), (self.width-35, bottom+7))
            self.text('X rail →', (self.width-100, bottom+13), color=MUTED, font=self.small, surface=bg)
        self.text('Left/right are fixed while facing into the aisle from its workstation (−X).',
                  (25, self.height-29), color=MUTED, font=self.small, surface=bg)
        self.static[self.selected] = bg
        return bg

    def draw(self, paused=False, speed=1.0):
        self.screen.blit(self.background(), (0, 0))
        schedule, now = self.schedule, self.schedule.now
        total = sum(t.source_lines for t in schedule.tasks)
        completed = schedule.completed_lines()
        status = 'COMPLETE' if now >= schedule.makespan else ('PAUSED' if paused else 'RUNNING')
        px = int(self.width*.64)
        self.text(status, (px, 137), color=GREEN, font=self.large)
        self.text(f'Time  {now:,.1f} s   |   {speed:g}×', (px, 176))
        self.text(f'Lines  {completed:,} / {total:,}', (px, 204))
        self.text(f'Pending  {total-completed:,}', (px, 232), color=MUTED)
        for name in self.names:
            crane = schedule.cranes[name]
            state = schedule.state(name)
            pos = state['position']
            base = self.overview((pos[0], crane.home[1], 0))
            fork = self.overview(pos)
            color = GOLD if state['loaded'] else GREEN
            pygame.draw.line(self.screen, color, base, fork, 3)
            pygame.draw.circle(self.screen, color, base, 6)
            if state['loaded']:
                pygame.draw.rect(self.screen, GOLD, (fork[0]-4, fork[1]-4, 8, 8))
        name = self.names[self.selected]
        crane = schedule.cranes[name]
        state = schedule.state(name)
        x, y, z = state['position']
        job = state['job']
        self.text(f'{name}   ·   {crane.workstation}   ·   {state["stage"].replace("_", " ")}', (25, 394), color=GREEN)
        self.text(f'X {x:.2f} m     Y fork {y-crane.home[1]:+.2f} m     Z {z:.2f} m     '
                  f'Carried: {job["tote_id"] if job and state["loaded"] else "empty"}', (25, 423))
        if job:
            self.text(f'Store {job["store_id"]}   SKU {job["sku"]}   {job["covered_lines"]} lines   '
                      f'{job["slot_address"]} ({job["rack_side"]})', (25, 452), color=MUTED)
        for side in (0, 1):
            point = self.elevation((x, y, z), side)
            bottom = self.elevation((x, y, 0), side)
            pygame.draw.line(self.screen, GREEN, (point[0], bottom[1]-110), (point[0], bottom[1]), 2)
            pygame.draw.rect(self.screen, GOLD if state['loaded'] else GREEN,
                             (point[0]-8, point[1]-5, 16, 10), 2)
            if job and side == (0 if job['rack_side'] == 'left' else 1):
                target = self.elevation((job['slot_x_m'], job['slot_y_m'], job['slot_z_m']), side)
                pygame.draw.circle(self.screen, GOLD, target, 11, 2)
            # Fork extension is perpendicular to this elevation; the signed Y value and overview show it.
        pygame.display.flip()


def play(schedule, map_data, totes, limit, title='ASRS', max_frames=None):
    renderer = Renderer(schedule, map_data, totes, title)
    clock = pygame.time.Clock()
    paused, speed, frames = False, schedule.config['display']['time_scale'], 0
    end = min(limit, schedule.makespan)
    reason = None
    try:
        running = True
        while running:
            dt = min(clock.tick(schedule.config['display']['fps'])/1000, .1)
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_SPACE:
                        paused = not paused
                    elif event.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                        speed = min(speed*2, 100000)
                    elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        speed = max(speed/2, .125)
                    elif event.key == pygame.K_TAB:
                        renderer.selected = (renderer.selected+1)%len(renderer.names)
                    elif pygame.K_1 <= event.key <= pygame.K_9:
                        renderer.selected = min(event.key-pygame.K_1, len(renderer.names)-1)
                    elif event.key == pygame.K_n and paused:
                        index = bisect_right(schedule.events, schedule.now)
                        if index < len(schedule.events):
                            schedule.advance(min(end, schedule.events[index]))
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    for index, rect in enumerate(renderer.buttons):
                        if rect.collidepoint(event.pos):
                            renderer.selected = index
            if not running:
                break
            if not paused:
                schedule.advance(min(end, schedule.now+dt*speed))
            renderer.draw(paused, speed)
            frames += 1
            if schedule.now >= end:
                paused = True
            if max_frames is not None and frames >= max_frames:
                break
        if schedule.now < end:
            reason = 'window_closed_before_completion'
        elif end < schedule.makespan:
            reason = 'simulation_time_limit_with_unfinished_tasks'
    finally:
        pygame.quit()
    return reason
