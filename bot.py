import os
import uuid
import asyncio
import subprocess
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

print("Loading Whisper...")
import whisper
whisper_model = whisper.load_model("base")

# Fallback translator (if no Gemini key)
from transformers import pipeline
nllb_translator = None

import edge_tts
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_GEMINI = os.getenv("GEMINI_KEY")  # optional global key

# user_id -> api_key
user_keys = {}

def run_cmd(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def download_video(url, out_path):
    run_cmd(["yt-dlp", "-f", "bv*+ba/best", "--merge-output-format", "mp4", "-o", out_path, url])

def extract_audio(video, audio):
    run_cmd(["ffmpeg", "-y", "-i", video, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio])

def translate_nllb(text):
    global nllb_translator
    if not nllb_translator:
        nllb_translator = pipeline("translation", model="facebook/nllb-200-distilled-600M",
                                   src_lang="eng_Latn", tgt_lang="mya_Mymr", device=-1)
    parts = [text[i:i+400] for i in range(0, len(text), 400)]
    return " ".join([nllb_translator(p, max_length=512)[0]['translation_text'] for p in parts])

def translate_gemini(text, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")
    # chunk to avoid limits
    chunks = [text[i:i+3000] for i in range(0, len(text), 3000)]
    results = []
    for ch in chunks:
        prompt = ("အောက်ပါ English transcript ကို သဘာဝကျပြီး စိတ်လှုပ်ရှားဖွယ်ကောင်းသော မြန်မာဘာသာစကားသို့ ပြန်ဆိုပါ။ "
                  "စာသားကို မတိုစေဘဲ မူရင်းအတိုင်း အပြည့်အစုံ ပြန်ပေးပါ။ Dialogues နဲ့ tone ကို ထိန်းပါ။\n\n" + ch)
        resp = model.generate_content(prompt)
        results.append(resp.text.strip())
    return "\n".join(results)

async def tts_save(text, voice, out_path):
    await edge_tts.Communicate(text, voice).save(out_path)

def get_duration(path):
    out = subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path])
    return float(out.decode().strip())

def process_file(input_video, user_id):
    tmp = f"/tmp/{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    video_path = f"{tmp}/in.mp4"
    if os.path.exists(input_video):
        run_cmd(["cp", input_video, video_path])
    else:
        download_video(input_video, video_path)

    audio_path = f"{tmp}/audio.wav"
    extract_audio(video_path, audio_path)

    transcript_en = whisper_model.transcribe(audio_path)["text"]

    api_key = user_keys.get(user_id) or DEFAULT_GEMINI
    if api_key:
        transcript_my = translate_gemini(transcript_en, api_key)
    else:
        transcript_my = translate_nllb(transcript_en)

    tts_path = f"{tmp}/voice.mp3"
    asyncio.run(tts_save(transcript_my, "my-MM-NilarNeural", tts_path))

    vd = get_duration(video_path)
    ad = get_duration(tts_path)
    atempo = max(0.5, min(2.0, ad/vd))

    final_path = f"{tmp}/final.mp4"
    run_cmd([
        "ffmpeg","-y","-i",video_path,"-i",tts_path,
        "-filter_complex", f"[1:a]atempo={atempo}[a]",
        "-map","0:v","-map","[a]",
        "-vf","eq=brightness=0.03:contrast=1.07",
        "-c:v","libx264","-c:a","aac","-shortest", final_path
    ])
    return final_path, transcript_en, transcript_my

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "မင်္ဂလာပါ! 🎙️\n\n1. /setkey YOUR_GEMINI_API_KEY  (ကိုယ့် key ထည့်ပါ)\n2. Video သို့မဟုတ် TikTok/YouTube link ပို့ပါ\nBot က auto dub လုပ်ပေးမယ်။"
    )

async def setkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: /setkey AIzaSy...")
        return
    key = context.args[0].strip()
    user_keys[update.effective_user.id] = key
    # test key
    try:
        genai.configure(api_key=key)
        genai.GenerativeModel("gemini-1.5-flash").generate_content("hi")
        await update.message.reply_text("✅ Gemini API key သိမ်းပြီးပါပြီ! အခု video ပို့လို့ရပါပြီ။")
    except Exception as e:
        await update.message.reply_text(f"❌ Key မမှန်ပါ: {e}")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = update.effective_user.id
    if not (user_keys.get(user_id) or DEFAULT_GEMINI):
        await msg.reply_text("⚠️ Gemini API key မရှိသေးပါ။ /setkey YOUR_KEY နဲ့ အရင်ထည့်ပါ။")
        return

    status = await msg.reply_text("⏳ Downloading...")
    try:
        if msg.video or msg.document:
            file = await (msg.video or msg.document).get_file()
            local = f"/tmp/{uuid.uuid4().hex}.mp4"
            await file.download_to_drive(local)
            source = local
        elif msg.text and "http" in msg.text:
            source = msg.text.strip()
        else:
            await status.edit_text("Video သို့မဟုတ် link ပို့ပါ")
            return

        await status.edit_text("🧠 Transcribing...")
        loop = asyncio.get_event_loop()
        final_path, en, my = await loop.run_in_executor(None, process_file, source, user_id)

        await status.edit_text("📤 Uploading...")
        await msg.reply_video(video=open(final_path, "rb"), caption=f"✅ Done!\n\n{my[:200]}...")
        await status.delete()
    except Exception as e:
        logging.exception(e)
        await status.edit_text(f"❌ Error: {e}")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("BOT_TOKEN မရှိပါ")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setkey", setkey))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.TEXT, handle))
    print("Bot running...")
    app.run_polling()
