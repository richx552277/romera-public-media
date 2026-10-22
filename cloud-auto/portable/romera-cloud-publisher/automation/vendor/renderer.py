# Only two draw calls removed from the frozen renderer.
def render(p, out):
    out.mkdir(parents=True, exist_ok=True)
    video = out / 'video.mp4'
    if p['source'] == 'archived_pilot_v1.0':
        source = confined(ROOT / 'inputs/romera-pilots', p['source_video'])
        if sha(source) != p['source_sha256']:
            raise ValueError('Source hash mismatch')
        shutil.copyfile(source, video)
        write(out / 'production.json', {'operation': 'reuse_byte_identical_archived_pilot', 'sha256': sha(video), 'source_sha256': p['source_sha256'], 'at': now()})
        return video
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    import wave
    assets = ROOT / 'inputs/romera-settings-v1.1/assets'
    font_path = assets / 'fonts' / ('NotoSansCJKjp-Medium.otf' if p['language'] == 'ja' else 'NotoSansCJKkr-Medium.otf')
    fonts = {n: ImageFont.truetype(str(font_path), n) for n in [25, 32, 62]}
    frames = []
    for i, s in enumerate(p['scenes']):
        im = Image.new('RGB', (1080, 1920), '#F6F0E6')
        d = ImageDraw.Draw(im)
        d.text((140, 270), 'ROMERA / v1.1', font=fonts[32], fill='#544842')
        d.line((140, 325, 940, 325), fill='#D4C9BC', width=2)
        for j, line in enumerate(s['text'].splitlines()):
            if d.textlength(line, font=fonts[62]) > 800:
                raise ValueError('Caption exceeds safe width')
            d.text((540, 430 + 88 * j), line, font=fonts[62], anchor='mm', fill='#544842')
        asset = Image.open(confined(assets / 'characters', s['asset'])).convert('RGBA')
        asset = asset.crop(asset.getbbox())
        asset.thumbnail((540, 800), Image.Resampling.LANCZOS)
        im.paste(asset, ((1080 - asset.width) // 2, 700), asset)
        if s.get('clock'):
            d.text((790, 650), s['clock'], font=fonts[32], anchor='mm', fill='#A65F4F')
        frames.append(im)
        im.save(out / f'scene-{i + 1}.png')
    contact = Image.new('RGB', (1080, 480))
    for i, im in enumerate(frames):
        contact.paste(im.resize((270, 480)), (270 * i, 0))
    contact.save(out / 'storyboard.jpg', quality=95)
    sr = 48000
    t = np.arange(sr * p['duration']) / sr
    a = np.zeros(t.shape)
    for start, f in [(0.4, 261.63), (3.4, 329.63), (7.0, 293.66), (10.0, 220)]:
        dt = t - start
        env = np.where(dt >= 0, np.exp(-np.maximum(dt, 0) * 2.5), 0)
        a += 0.09 * np.sin(2 * np.pi * f * dt) * env
    a *= np.minimum(t / 0.05, 1) * np.minimum((p['duration'] - t) / 0.5, 1)
    audio = out / 'original-tones.wav'
    with wave.open(str(audio), 'wb') as w:
        w.setparams((2, 2, sr, 0, 'NONE', 'not compressed'))
        w.writeframes(np.repeat((a * 32767).astype('<i2')[:, None], 2, axis=1).tobytes())
    args = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '1080x1920', '-r', '30', '-i', 'pipe:0', '-i', audio, '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-threads', '4', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-t', p['duration'], '-movflags', '+faststart', video]
    with (out / 'render.log').open('w') as log:
        proc = subprocess.Popen([str(x) for x in args], stdin=subprocess.PIPE, stderr=log)
        try:
            for im, s in zip(frames, p['scenes']):
                for n in range(round(s['seconds'] * 30)):
                    frame = im.copy()
                    region = im.crop((240, 680, 840, 1520))
                    frame.paste('#F6F0E6', (240, 680, 840, 1520))
                    frame.paste(region, (240, 680 + round(3 * math.sin(n / 30 * 2))))
                    proc.stdin.write(frame.tobytes())
        finally:
            proc.stdin.close()
        if proc.wait(timeout=180):
            raise RuntimeError('Render failed; see render.log')
    write(out / 'production.json', {'operation': 'new_local_v1.1_render', 'at': now(), 'sha256': sha(video), 'font_sha256': sha(font_path), 'paid_services_called': False})
    start = 0
    sub = []

    def stime(s):
        ms = round(s * 1000)
        return f'{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}'
    for i, s in enumerate(p['scenes']):
        sub.append(f"{i + 1}\n{stime(start)} --> {stime(start + s['seconds'])}\n{s['text']}\n")
        start += s['seconds']
    (out / f"subtitles.{p['language']}.srt").write_text('\n'.join(sub), 'utf-8-sig')
    return video
