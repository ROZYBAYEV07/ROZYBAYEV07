#!/usr/bin/env python3
"""Собирает анимированное видео-сказку «Акула Мила находит друзей».

Фотографии страниц -> сцены с плавным движением камеры (Ken Burns),
женская озвучка (RHVoice, голос victoria), субтитры, тихая фоновая музыка.

Использование:
    python3 make_video.py <папка_с_фото> <выходной.mp4>

В папке должны лежать файлы 0.jpg (обложка) ... 5.jpg (развороты).
Нужны: ffmpeg, RHVoice-test (+ пакет rhvoice-russian), Pillow, numpy.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

W, H, FPS = 1920, 1080, 25
VOICE = "victoria"
SR = 24000
FONT = "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf"
FONT_TITLE = "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf"

# Обрезка книги на фото (доля от ширины/высоты исходного кадра, без учёта EXIF-поворота
# для разворотов; для обложки — после EXIF-поворота).
CROPS = {
    0: (0.190, 0.232, 0.757, 0.689),
    1: (0.104, 0.146, 0.705, 0.862),
    2: (0.146, 0.080, 0.818, 0.856),
    3: (0.113, 0.080, 0.783, 0.860),
    4: (0.156, 0.084, 0.831, 0.856),
    5: (0.113, 0.063, 0.778, 0.858),
}

# Сцены: изображение, части (что говорим, какие строки субтитров, куда смотрит камера).
# focus = (cx, cy, zoom) в долях изображения.
STORY = [
    {"img": 0, "title": True, "parts": [
        {"say": "Акула Мила находит друзей.", "sub": None, "focus": (0.5, 0.42, 1.12)},
    ]},
    {"img": 1, "parts": [
        {"sub": ["На глубине среди кораллов", "Акула Мила обитала."], "focus": (0.32, 0.38, 1.55)},
        {"sub": ["Как покидала домик кроха,", "Так начиналась суматоха."], "focus": (0.30, 0.45, 1.70)},
        {"sub": ["Кальмар пускался наутёк.", "– Спаси! – визжал морской конёк."], "focus": (0.70, 0.40, 1.60)},
        {"sub": ["От ласковой её улыбки", "Дрожали все в округе рыбки."], "focus": (0.68, 0.62, 1.70)},
    ]},
    {"img": 2, "parts": [
        {"sub": ["Но что всего обидней было –", "Ей дали прозвище Зубила."], "focus": (0.36, 0.38, 1.55)},
        {"sub": ["И Мила каждый день мечтала,", "Чтобы зубов совсем не стало."], "focus": (0.55, 0.55, 1.45)},
        {"sub": ["Тогда вся живность в океане", "Её бояться перестанет."], "focus": (0.66, 0.38, 1.60)},
        {"sub": ["Акулы нет нигде добрей.", "Но как ей завести друзей?"], "focus": (0.62, 0.55, 1.75)},
    ]},
    {"img": 3, "parts": [
        {"sub": ["И вот однажды на полянке", "Играли рыбки в догонялки,"], "focus": (0.34, 0.36, 1.55)},
        {"sub": ["Морские звёзды танцевали", "И ничего не замечали."], "focus": (0.62, 0.50, 1.50)},
        {"sub": ["Вдруг появился странный зверь.", "Что будет с крошками теперь?"], "focus": (0.68, 0.62, 1.65)},
        {"sub": ["Он в маске, а нога как лист...", "Да это же аквалангист!"], "focus": (0.30, 0.62, 1.75)},
    ]},
    {"img": 4, "parts": [
        {"say": "Ой-ой-ой! Спасите!", "sub": ["– Ой-ой-ой! Спасите!"], "focus": (0.30, 0.36, 1.65)},
        {"sub": ["В его сачке уже сардины,", "А рака посадил в корзину,"], "focus": (0.34, 0.60, 1.55)},
        {"sub": ["Звезду за щупальце берёт.", "Ах, кто же малышей спасёт?"], "focus": (0.30, 0.66, 1.70)},
        {"say": "Немедленно прекрати! А то мы тебе покажем!",
         "sub": ["– Немедленно прекрати!", "А то мы тебе покажем!"], "focus": (0.70, 0.42, 1.60)},
        {"sub": ["Вот Мила поплыла вперёд", "И улыбнулась во весь рот."], "focus": (0.68, 0.62, 1.55)},
        {"sub": ["Аквалангиста след простыл!", "Корзинки все он позабыл."], "focus": (0.72, 0.58, 1.75)},
    ]},
    {"img": 5, "parts": [
        {"sub": ["Кричит морская детвора:", "– Мы на свободе вновь, ура!"], "focus": (0.34, 0.40, 1.55)},
        {"sub": ["Прости нас, Мила, только ты", "Смогла спасти нас от беды!"], "focus": (0.30, 0.50, 1.70)},
        {"sub": ["С тех пор акулу не дразнили,", "А крепко-крепко полюбили."], "focus": (0.68, 0.42, 1.55)},
        {"sub": ["Довольна Мила: рядом с ней", "Есть целый океан друзей!"], "focus": (0.55, 0.50, 1.15)},
    ]},
    {"img": 5, "end": True, "parts": [
        {"say": "Конец.", "sub": None, "focus": (0.5, 0.5, 1.0)},
    ]},
]

LEAD_IN = 1.2      # пауза перед первой репликой сцены (камера на общем плане)
GAP = 0.9          # пауза между частями
TAIL = 1.3         # пауза в конце сцены
XFADE = 1.0        # длительность перехода между сценами


def tts(text, path):
    subprocess.run(["RHVoice-test", "-p", VOICE, "-o", path],
                   input=text.encode("utf-8"), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = path + ".pcm.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR),
                    "-af", "highpass=f=70,loudnorm=I=-18:TP=-2:LRA=7", out], check=True)
    with wave.open(out) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return data


def load_images(src):
    imgs = {}
    for i, box in CROPS.items():
        im = Image.open(os.path.join(src, f"{i}.jpg"))
        if i == 0:
            im = ImageOps.exif_transpose(im)
        else:
            im = im.copy()  # пиксели разворотов уже стоят правильно, EXIF-поворот игнорируем
        im = im.convert("RGB")
        w, h = im.size
        im = im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
        im = ImageEnhance.Color(im).enhance(1.12)
        im = ImageEnhance.Contrast(im).enhance(1.05)
        # скруглённые углы — прячем ковёр в углах страниц
        mask = Image.new("L", im.size, 0)
        r = int(min(im.size) * 0.045)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), r, fill=255)
        im.putalpha(mask)
        imgs[i] = im
    return imgs


def make_background(im):
    bg = im.convert("RGB").resize((W // 4, H // 4), Image.LANCZOS)
    bg = ImageOps.fit(bg, (W // 4, H // 4))
    bg = bg.filter(ImageFilter.GaussianBlur(14))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    tint = Image.new("RGB", bg.size, (10, 30, 70))
    bg = Image.blend(bg, tint, 0.35)
    return bg.resize((W, H), Image.BICUBIC)


def smooth(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def render_page(im, bg, cam, shadow):
    """cam = (cx, cy, zoom): точка изображения в центре кадра и увеличение."""
    iw, ih = im.size
    s0 = min(W * 0.90 / iw, H * 0.90 / ih)
    s = s0 * cam[2]
    # обратное аффинное преобразование: канва -> изображение
    a = 1 / s
    c = cam[0] * iw - (W / 2) / s
    f = cam[1] * ih - (H / 2) / s
    frame = bg.copy()
    sh = shadow.transform((W, H), Image.AFFINE, (a, 0, c - 14 / s, 0, a, f - 22 / s), Image.BILINEAR)
    frame.paste((0, 0, 0), (0, 0), sh)
    page = im.transform((W, H), Image.AFFINE, (a, 0, c, 0, a, f), Image.BICUBIC)
    frame.paste(page, (0, 0), page)
    return frame


def subtitle_layer(lines, font):
    if not lines:
        return None
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lh = int(font.size * 1.28)
    widths = [d.textlength(t, font=font) for t in lines]
    bw = int(max(widths)) + 90
    bh = lh * len(lines) + 46
    x0 = (W - bw) // 2
    y0 = H - bh - 46
    d.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), 28, fill=(8, 22, 52, 175))
    for k, t in enumerate(lines):
        tx = (W - widths[k]) / 2
        ty = y0 + 23 + k * lh
        d.text((tx + 2, ty + 3), t, font=font, fill=(0, 0, 0, 150))
        d.text((tx, ty), t, font=font, fill=(255, 250, 235, 255))
    return layer


def end_layer(font):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    t = "Конец"
    tw = d.textlength(t, font=font)
    x, y = (W - tw) / 2, H / 2 - font.size * 0.62
    for r in (10, 6):
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(glow).text((x, y), t, font=font, fill=(40, 120, 255, 200))
        layer = Image.alpha_composite(layer, glow.filter(ImageFilter.GaussianBlur(r)))
    d = ImageDraw.Draw(layer)
    d.text((x, y), t, font=font, fill=(255, 214, 70, 255), stroke_width=6, stroke_fill=(150, 40, 120, 255))
    return layer


def music(duration):
    """Тихий «подводный» эмбиент: мягкие аккорды + редкие пузырьки."""
    n = int(duration * SR)
    t = np.arange(n) / SR
    out = np.zeros(n, dtype=np.float32)
    chords = [[261.63, 329.63, 392.00], [220.00, 261.63, 329.63],
              [174.61, 220.00, 261.63], [196.00, 246.94, 293.66]]
    seg = 6.0
    for k in range(int(duration / seg) + 2):
        ch = chords[k % len(chords)]
        start = k * seg - 1.5
        a, b = max(0, int(start * SR)), min(n, int((start + seg + 3) * SR))
        if a >= b:
            continue
        tt = t[a:b] - start
        env = np.sin(np.pi * np.clip(tt / (seg + 3), 0, 1)) ** 2
        for j, fr in enumerate(ch):
            vib = 1 + 0.002 * np.sin(2 * np.pi * 0.3 * tt + j)
            out[a:b] += env * (np.sin(2 * np.pi * fr * vib * tt) * 0.5
                              + np.sin(2 * np.pi * fr * 2 * tt) * 0.08) / 3
        # колокольчик-арпеджио
        for j, fr in enumerate(ch + [ch[0] * 2]):
            st = start + 1.5 + j * 0.75
            p, q = int(st * SR), min(n, int((st + 2.5) * SR))
            if 0 <= p < q:
                ttt = t[p:q] - st
                out[p:q] += 0.10 * np.exp(-ttt * 2.2) * np.sin(2 * np.pi * fr * 2 * ttt)
    rng = np.random.default_rng(7)
    for st in rng.uniform(0, duration, int(duration / 2.5)):
        p = int(st * SR)
        L = int(0.09 * SR)
        if p + L >= n:
            continue
        tt = np.arange(L) / SR
        fr = rng.uniform(500, 1100)
        out[p:p + L] += 0.05 * np.exp(-tt * 35) * np.sin(2 * np.pi * (fr + 4000 * tt) * tt)
    out /= np.max(np.abs(out)) + 1e-9
    fade = int(2.5 * SR)
    out[:fade] *= np.linspace(0, 1, fade)
    out[-fade:] *= np.linspace(1, 0, fade)
    return out * 0.11


def main():
    src, dst = sys.argv[1], sys.argv[2]
    work = tempfile.mkdtemp(prefix="akula_")
    imgs = load_images(src)
    bgs = {k: make_background(v) for k, v in imgs.items()}
    shadows = {}
    for k, v in imgs.items():
        a = v.getchannel("A").point(lambda p: int(p * 0.55))
        pad = 40
        big = Image.new("L", (v.size[0] + 2 * pad, v.size[1] + 2 * pad), 0)
        big.paste(a, (pad, pad))
        big = big.filter(ImageFilter.GaussianBlur(18)).crop((pad, pad, pad + v.size[0], pad + v.size[1]))
        shadows[k] = Image.merge("RGBA", (big, big, big, big))
    font = ImageFont.truetype(FONT, 50)
    font_end = ImageFont.truetype(FONT_TITLE, 210)
    endl = end_layer(font_end)

    # 1. Озвучка и тайминги
    voice_track = []
    scenes = []
    clock = 0.0
    idx = 0
    for sc in STORY:
        parts = []
        t = LEAD_IN
        for p in sc["parts"]:
            text = p.get("say") or " ".join(p["sub"])
            audio = tts(text, os.path.join(work, f"v{idx}.wav"))
            idx += 1
            dur = len(audio) / SR
            parts.append({"start": t, "end": t + dur, "sub": p["sub"], "focus": p["focus"]})
            voice_track.append((clock + t, audio))
            t += dur + GAP
        length = t - GAP + TAIL
        scenes.append({"img": sc["img"], "start": clock, "len": length, "parts": parts,
                       "end": sc.get("end", False), "title": sc.get("title", False)})
        clock += length - XFADE
    total = clock + XFADE

    # 2. Аудиодорожка
    n = int(total * SR) + SR
    mix = music(n / SR)
    for st, a in voice_track:
        p = int(st * SR)
        mix[p:p + len(a)] += a[: max(0, n - p)] * 0.95
    mix = np.clip(mix, -1, 1)
    wav_path = os.path.join(work, "mix.wav")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype(np.int16).tobytes())

    # 3. Камера
    def camera(sc, lt):
        full = (0.5, 0.5, 1.0)
        keys = [(0.0, full)]
        prev_end = LEAD_IN * 0.3
        for p in sc["parts"]:
            keys.append((max(prev_end, p["start"] - 0.6), keys[-1][1]))
            keys.append((p["start"] + min(2.6, (p["end"] - p["start"]) * 0.7), p["focus"]))
            prev_end = p["end"]
        lastf = keys[-1][1]
        keys.append((sc["len"], (lastf[0], lastf[1], lastf[2] * 1.05)))
        if sc["title"]:
            keys = [(0.0, (0.5, 0.5, 1.0)), (sc["len"], (0.5, 0.42, 1.12))]
        if sc["end"]:
            keys = [(0.0, (0.5, 0.5, 1.15)), (sc["len"], (0.5, 0.5, 1.0))]
        for (t0, a), (t1, b) in zip(keys, keys[1:]):
            if lt <= t1:
                u = smooth((lt - t0) / (t1 - t0)) if t1 > t0 else 1
                return tuple(a[i] + (b[i] - a[i]) * u for i in range(3))
        return keys[-1][1]

    sub_cache = {}

    def scene_frame(sc, lt):
        img = sc["img"]
        fr = render_page(imgs[img], bgs[img], camera(sc, lt), shadows[img]).convert("RGBA")
        for k, p in enumerate(sc["parts"]):
            if p["sub"] and p["start"] - 0.35 <= lt <= p["end"] + 0.45:
                key = (id(sc), k)
                if key not in sub_cache:
                    sub_cache[key] = subtitle_layer(p["sub"], font)
                alpha = min(1, (lt - p["start"] + 0.35) / 0.35, (p["end"] + 0.45 - lt) / 0.35)
                layer = sub_cache[key]
                if alpha < 1:
                    layer = layer.copy()
                    layer.putalpha(layer.getchannel("A").point(lambda v: int(v * alpha)))
                fr = Image.alpha_composite(fr, layer)
        if sc["end"]:
            dark = Image.new("RGBA", (W, H), (0, 10, 30, int(110 * smooth(lt / 1.2))))
            fr = Image.alpha_composite(fr, dark)
            e = endl.copy()
            e.putalpha(e.getchannel("A").point(lambda v: int(v * smooth((lt - 0.6) / 1.0))))
            fr = Image.alpha_composite(fr, e)
        return fr.convert("RGB")

    # 4. Кадры -> ffmpeg
    nframes = int(total * FPS)
    ff = subprocess.Popen([
        "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
        "-r", str(FPS), "-i", "-", "-i", wav_path,
        "-vf", f"fade=t=in:st=0:d=1,fade=t=out:st={total - 1.5:.2f}:d=1.5",
        "-af", f"afade=t=out:st={total - 1.5:.2f}:d=1.5",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", dst,
    ], stdin=subprocess.PIPE)
    for fi in range(nframes):
        t = fi / FPS
        active = [sc for sc in scenes if sc["start"] <= t < sc["start"] + sc["len"]]
        frames = [(sc, scene_frame(sc, t - sc["start"])) for sc in active]
        if len(frames) == 1:
            out = frames[0][1]
        else:
            nxt = frames[-1][0]
            u = smooth((t - nxt["start"]) / XFADE)
            out = Image.blend(frames[0][1], frames[-1][1], u)
        ff.stdin.write(out.tobytes())
        if fi % 250 == 0:
            print(f"кадр {fi}/{nframes}", flush=True)
    ff.stdin.close()
    ff.wait()
    print(json.dumps({"duration": round(total, 1), "output": dst}, ensure_ascii=False))


if __name__ == "__main__":
    main()
