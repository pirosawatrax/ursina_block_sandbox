import os
# 日本語入力(IME)がキーを横取りしないようにする（ursinaを読み込む前に設定する）
os.environ['XMODIFIERS'] = '@im=none'

import json
import math
import random
import struct
import wave
from ursina import *
from ursina.prefabs.first_person_controller import FirstPersonController
from ursina.shaders import lit_with_shadows_shader

app = Ursina()

# ---------------------------------------------------------------
# 効果音・BGMの生成（初回起動時に sounds/ フォルダへ wav を自動作成）
# 自分の音源を使いたい場合は、sounds/ 内の同名ファイルを差し替えるだけ
# ---------------------------------------------------------------
RATE = 22050


def write_wav(path, samples):
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        data = b''.join(
            struct.pack('<h', int(max(-1.0, min(1.0, s)) * 32767)) for s in samples
        )
        f.writeframes(data)


def tone(freq, dur, vol=0.4, decay=6.0, harmonics=(1.0, 0.3, 0.1)):
    """減衰する柔らかい音（倍音つきサイン波）"""
    n = int(RATE * dur)
    out = []
    for i in range(n):
        t = i / RATE
        env = math.exp(-decay * t)
        # 末尾でプツッと鳴らないよう、最後の短い区間で音量を絞る
        fade = min(1.0, (n - i) / (RATE * 0.02))
        s = sum(h * math.sin(2 * math.pi * freq * (k + 1) * t)
                for k, h in enumerate(harmonics))
        out.append(s * env * fade * vol)
    return out


def make_sounds():
    folder = application.asset_folder / 'sounds'
    folder.mkdir(exist_ok=True)

    # ブロックを置く音: 短くて高めの「ポン」
    place = folder / 'place.wav'
    if not place.exists():
        write_wav(place, tone(520, 0.12, vol=0.5, decay=28, harmonics=(1.0, 0.4)))

    # ブロックを壊す音: ノイズ + 低い音の「ガッ」
    destroy_wav = folder / 'break.wav'
    if not destroy_wav.exists():
        rnd = random.Random(1)
        n = int(RATE * 0.18)
        out = []
        for i in range(n):
            t = i / RATE
            env = math.exp(-22 * t)
            noise = rnd.uniform(-1, 1)
            low = math.sin(2 * math.pi * 110 * t)
            out.append((0.5 * noise + 0.5 * low) * env * 0.6)
        write_wav(destroy_wav, out)

    # ブロックが消える音: 上昇する3音の「ピロリン」
    vanish = folder / 'vanish.wav'
    if not vanish.exists():
        seq = (tone(660, 0.07, vol=0.45, decay=14, harmonics=(1.0, 0.3))
               + tone(880, 0.07, vol=0.45, decay=14, harmonics=(1.0, 0.3))
               + tone(1320, 0.22, vol=0.45, decay=9, harmonics=(1.0, 0.3)))
        write_wav(vanish, seq)

    # BGM: ペンタトニックスケールのゆったりしたループ（約16秒）
    bgm = folder / 'bgm.wav'
    if not bgm.exists():
        scale = [261.63, 293.66, 329.63, 392.00, 440.00, 523.25]  # C D E G A C
        rnd = random.Random(7)
        note_len = 0.5
        total = int(RATE * note_len * 32)
        mix = [0.0] * total
        pos = 0
        for step in range(32):
            # メロディ（たまに休符）
            if rnd.random() > 0.2:
                m = tone(rnd.choice(scale), note_len * 2, vol=0.22, decay=3.0)
                for j, s in enumerate(m):
                    if pos + j < total:
                        mix[pos + j] += s
            # 低音（4拍ごと）
            if step % 4 == 0:
                b = tone(scale[0] / 2 if step % 8 == 0 else scale[3] / 2,
                         note_len * 4, vol=0.25, decay=1.2, harmonics=(1.0, 0.2))
                for j, s in enumerate(b):
                    if pos + j < total:
                        mix[pos + j] += s
            pos += int(RATE * note_len)
        write_wav(bgm, mix)


make_sounds()
snd_place = Audio('sounds/place.wav', autoplay=False, volume=0.7)
snd_break = Audio('sounds/break.wav', autoplay=False, volume=0.7)
snd_vanish = Audio('sounds/vanish.wav', autoplay=False, volume=0.8)
bgm = Audio('sounds/bgm.wav', loop=True, autoplay=True, volume=0.35)


def play_sfx(snd):
    snd.pitch = random.uniform(0.9, 1.1)   # 毎回少し音程を変えて単調さを減らす
    snd.play()


# ---------------------------------------------------------------
# ゲーム本体
# ---------------------------------------------------------------
# ブロックの種類（名前, 色）。地面の塗り分けにも、持ち物にも同じ5色を使う
BLOCKS = [
    ('red', color.red),
    ('yellow', color.yellow),
    ('green', color.green),
    ('blue', color.azure),
    ('violet', color.violet),
]

WORLD_SIZE = 25       # 地面の一辺（ブロック数）
BOTTOM_Y = -4         # 一番下の層（壊せない岩盤）。地面は 0 〜 BOTTOM_Y の5層
START_COUNT = 0       # 最初に持っている各色のブロック数（99 にすれば最初から置ける）
DAY_SECONDS = 60
SHADOWS = True        # 影が重い場合は False に

SCORE_PER_LEVEL = 100   # 高さ1段あたりの得点
SAVE_FILE = application.asset_folder / 'savegame.json'

game_over = False                     # ESCで終了すると True（操作できなくなる）
selected = 0                          # 今選んでいる色の番号
inventory = [START_COUNT] * len(BLOCKS)   # 色ごとの所持数

sky = Sky()
sun = DirectionalLight(shadows=SHADOWS)
sun.rotation_x = 50
sun.rotation_y = 30
ambient = AmbientLight()


class Voxel(Button):
    def __init__(self, position=(0, 0, 0), block_color=color.white):
        super().__init__(
            parent=scene,
            position=position,
            model='cube',
            origin_y=0.5,
            texture='white_cube',
            color=block_color,
            highlight_color=color.lime,
            shader=lit_with_shadows_shader,
        )
        self.grid_pos = (round(position[0]), round(position[1]), round(position[2]))

    def input(self, key):
        # ロードなどで破棄されたブロックは操作しない
        if hasattr(self, 'destroy_source'):
            return

        if not self.hovered:
            return

        # プレイヤーからブロックまでの距離を制限
        distance = (Vec3(*self.grid_pos) - player.position).length()

        if distance > 5:
            return

        if key == 'left mouse down':
            place_block(self.position + mouse.normal)

        if key == 'right mouse down':
            remove_block(self.grid_pos)




   
   
# ---------------------------------------------------------------
# ワールド管理
# world: 座標 -> {'kind':色の番号, 'entity':表示用Voxel or None, 'bedrock':bool}
# 全ブロックを表示すると重いので、空気に接している（見える）ブロックだけを表示する。
# 掘ると、新しく見えるようになったブロックをその場で表示する。
# ---------------------------------------------------------------
DIRS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
world = {}


def solid(p):
    # 岩盤より下は「埋まっている」扱い（底面を描かなくて済む）
    return p in world or p[1] < BOTTOM_Y


def exposed(p):
    return any(not solid((p[0] + dx, p[1] + dy, p[2] + dz)) for dx, dy, dz in DIRS)


def spawn(p):
    data = world[p]
    if data['entity'] is None and exposed(p):
        # 岩盤は灰色、それ以外は通常の5色
        if data['bedrock']:
            block_color = color.dark_gray
        else:
            block_color = BLOCKS[data['kind']][1]

        data['entity'] = Voxel(
            position=p,
            block_color=block_color
        )

def add_block(p, kind, bedrock=False):
    world[p] = {'kind': kind, 'entity': None, 'bedrock': bedrock}
    spawn(p)


def remove_block(p):
    """ブロックを壊す。壊した色は持ち物に入り、その色が選択される"""
    global selected
    data = world.get(p)
    if game_over or data is None or data['bedrock']:
        return
    del world[p]
    if data['entity'] is not None:
        destroy(data['entity'])
    inventory[data['kind']] += 1
    selected = data['kind']
    refresh_hotbar()
    play_sfx(snd_break)
    # 隣のブロックが新しく見えるようになるので表示する
    for dx, dy, dz in DIRS:
        n = (p[0] + dx, p[1] + dy, p[2] + dz)
        if n in world:
            spawn(n)
    update_score()


def vanish(cells):
    """指定したブロックを持ち物に入れずに消す（縮むアニメーション付き）"""
    affected = set()
    for p in cells:
        data = world.pop(p, None)
        if data is not None and data['entity'] is not None:
            e = data['entity']
            e.collision = False
            e.animate_scale(0.05, duration=0.2)
            destroy(e, delay=0.2)
        for dx, dy, dz in DIRS:
            affected.add((p[0] + dx, p[1] + dy, p[2] + dz))
    # 隣のブロックが新しく見えるようになるので表示する
    for n in affected:
        if n in world:
            spawn(n)


def build_world():
    half = WORLD_SIZE // 2
    for x in range(-half, half + 1):
        for z in range(-half, half + 1):
            for y in range(0, BOTTOM_Y - 1, -1):
                # 5色の塗り分け: 面で接するブロックは必ず別の色になる
                kind = (x + 2 * y + 3 * z) % 5
                world[(x, y, z)] = {'kind': kind, 'entity': None,
                                    'bedrock': y == BOTTOM_Y}
    # 全部登録してから、見えるものだけ表示する
    for p in list(world):
        spawn(p)


build_world()

player = FirstPersonController(position=(0, 2, 0))
player.speed = 6
player.jump_height = 2.2     # ブロック1〜2個分を飛び越えられる高さ


def place_block(pos):
    """選択中の色のブロックを1個消費して置く"""
    p = (round(pos[0]), round(pos[1]), round(pos[2]))
    if game_over or p in world or inventory[selected] <= 0:
        return
    # ブロックは p[1]-1 〜 p[1]、プレイヤーは player.y 〜 player.y+2 の範囲
    overlap = (abs(p[0] - player.x) < 0.8 and abs(p[2] - player.z) < 0.8
               and player.y < p[1] and player.y + 2 > p[1] - 1)
    if overlap:
        return
    add_block(p, selected)
    inventory[selected] -= 1
    refresh_hotbar()
    # 面で接する隣のブロックに同じ色があれば、置いたブロックと一緒に消える
    # （例: 赤・空間・赤 の間に赤を置くと、3つとも消える）
    same = []
    for dx, dy, dz in DIRS:
        n = (p[0] + dx, p[1] + dy, p[2] + dz)
        if n in world and world[n]['kind'] == selected and not world[n]['bedrock']:
            same.append(n)
    if same:
        vanish([p] + same)
        play_sfx(snd_vanish)
    else:
        play_sfx(snd_place)
    update_score()


# ---------------------------------------------------------------
# UI: 画面下のホットバー（色・数字キー・所持数）
# ---------------------------------------------------------------
Entity(parent=camera.ui, model='quad', scale=0.008, color=color.black)   # 照準
Text(text='[F2] Save  [F3] Load  [M] BGM  [ESC] Finish', position=(-0.85, 0.47), scale=1.1)
score_text = Text(text='', position=(0.42, 0.47), scale=1.3)
toast_text = Text(text='', position=(0, 0.35), origin=(0, 0), scale=2)
toast_token = 0

# 終了画面（ESCで表示）
final_panel = Entity(parent=camera.ui, enabled=False)
Entity(parent=final_panel, model='quad', scale=(1.1, 0.6), z=-1, color=Color(0, 0, 0, 0.88))
final_text = Text(text='', parent=final_panel, origin=(0, 0), position=(0, 0.1, -1.1), scale=2.2)
Text(text='[F3] Load save    [Q] Quit', parent=final_panel, origin=(0, 0),
     position=(0, -0.2, -1.1), scale=1.2)

slot_bg = []
slot_count = []
for i, (name, col) in enumerate(BLOCKS):
    x = (i - (len(BLOCKS) - 1) / 2) * 0.12
    slot_bg.append(Entity(parent=camera.ui, model='quad', scale=0.095,
                          position=(x, -0.40, 0.01), color=color.black66))
    Entity(parent=camera.ui, model='quad', scale=0.07, position=(x, -0.40), color=col)
    Text(text=str(i + 1), parent=camera.ui, position=(x - 0.04, -0.36, -0.02),
         scale=1, origin=(-0.5, 0.5))
    slot_count.append(Text(text='0', parent=camera.ui, position=(x, -0.475, -0.02),
                           scale=1.3, origin=(0, 0)))


def refresh_hotbar():
    for i in range(len(BLOCKS)):
        slot_count[i].text = f'x{inventory[i]}'
        slot_bg[i].color = color.white if i == selected else color.black66


def current_height():
    """いちばん高いブロックの高さ（地面の上面=0）"""
    return max(0, max(p[1] for p in world))


def update_score():
    h = current_height()
    score_text.text = f'HEIGHT {h}   SCORE {h * SCORE_PER_LEVEL}'


def toast(msg, seconds=1.5):
    global toast_token
    toast_token += 1
    my_token = toast_token
    toast_text.text = msg

    def clear():
        if toast_token == my_token:
            toast_text.text = ''
    invoke(clear, delay=seconds)


def save_game():
    data = {
        'version': 1,
        'blocks': [[p[0], p[1], p[2], d['kind'], 1 if d['bedrock'] else 0]
                   for p, d in world.items()],
        'inventory': list(inventory),
        'selected': selected,
        'player': [player.x, player.y, player.z],
    }
    try:
        SAVE_FILE.write_text(json.dumps(data))
        toast('Saved')
    except OSError as e:
        print('save failed:', e)
        toast('Save failed')


def load_game():
    global selected, game_over
    # 先にファイルを全部読んで検証し、問題なければ世界を入れ替える（壊れたデータで世界が崩れないように）
    try:
        data = json.loads(SAVE_FILE.read_text())
        blocks = {}
        for x, y, z, kind, bed in data['blocks']:
            if not 0 <= kind < len(BLOCKS):
                raise ValueError('bad kind')
            blocks[(int(x), int(y), int(z))] = {'kind': int(kind), 'entity': None,
                                                'bedrock': bool(bed)}
        inv = [int(v) for v in data['inventory']]
        if len(inv) != len(BLOCKS) or not blocks:
            raise ValueError('bad data')
        sel = int(data['selected']) % len(BLOCKS)
        px, py, pz = (float(v) for v in data['player'])
    except FileNotFoundError:
        toast('No save file')
        return
    except (OSError, ValueError, KeyError, TypeError) as e:
        print('load failed:', e)
        toast('Load failed')
        return
    # 今の世界を片付けて、保存した世界に入れ替える
    for d in world.values():
        if d['entity'] is not None:
            destroy(d['entity'])
    world.clear()
    world.update(blocks)
    for p in list(world):
        spawn(p)
    inventory[:] = inv
    selected = sel
    player.position = (px, py, pz)
    # 終了画面からのロードは、ゲーム再開になる
    game_over = False
    final_panel.enabled = False
    player.enabled = True
    mouse.locked = True
    refresh_hotbar()
    update_score()
    toast('Loaded')


def finish_game():
    """ESCで終了: 最終得点を表示して、操作を止める"""
    global game_over
    if game_over:
        return
    game_over = True
    h = current_height()
    score = h * SCORE_PER_LEVEL
    print(f'FINAL: height={h} score={score}')    # 画面が閉じても結果が分かるように
    final_text.text = f'GAME OVER\nHEIGHT {h}\nFINAL SCORE {score}'
    final_panel.enabled = True
    player.enabled = False
    mouse.locked = False


def select(index):
    global selected
    selected = index % len(BLOCKS)
    refresh_hotbar()


def input(key):
    if key.isdigit() and 1 <= int(key) <= len(BLOCKS):
        select(int(key) - 1)
    if key == 'scroll up':
        select(selected - 1)
    if key == 'scroll down':
        select(selected + 1)
    if key == 'm':
        bgm.volume = 0 if bgm.volume > 0 else 0.35
    if key == 'escape':
        finish_game()
    if key == 'f2' and not game_over:
        save_game()
    if key == 'f3':
        load_game()
    if key == 'q' and game_over:
        application.quit()


# 影の描画範囲をプレイヤーの周囲に合わせる
shadow_area = Entity(model='cube', scale=(40, 12, 40), color=color.clear)
shadow_timer = 0.0


def update():
    global shadow_timer
    # 昼夜サイクル
    sun.rotation_x = (sun.rotation_x + 360 / DAY_SECONDS * time.dt) % 360
    daylight = max(0.0, math.sin(math.radians(sun.rotation_x)))
    k = 0.12 + 0.88 * daylight
    sky.color = Color(k, k, min(1.0, k * 1.05), 1)
    sun.color = Color(1, 0.6 + 0.4 * daylight, 0.4 + 0.6 * daylight, 1)
    a = 0.25 + 0.35 * daylight
    ambient.color = Color(a, a, a * 1.1, 1)

    # 0.5秒ごとに影の範囲を更新
    if SHADOWS:
        shadow_timer += time.dt
        if shadow_timer > 0.5:
            shadow_timer = 0.0
            shadow_area.position = (player.x, 0, player.z)
            try:
                sun.update_bounds(shadow_area)
            except Exception:
                pass

    # 地面の外に落ちたらスタート地点へ戻す
    if player.y < -15:
        player.position = (0, 2, 0)


refresh_hotbar()
update_score()

app.run()
