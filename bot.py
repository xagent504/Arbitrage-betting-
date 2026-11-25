import io
import logging
import os
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PHOTO_TYPES = {
    "type_self": ("নিজের ছবি", "self"),
    "type_family": ("পরিবার", "family"),
    "type_friends": ("বন্ধুদের সাথে", "friends"),
}

PHOTO_MOODS = {
    "mood_happy": ("হাসিখুশি", "happy"),
    "mood_emotional": ("আবেগী", "emotional"),
    "mood_travel": ("ভ্রমণ বা আউটডোর", "travel"),
}


def photo_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("নিজের ছবি", callback_data="type_self"),
                InlineKeyboardButton("পরিবার", callback_data="type_family"),
            ],
            [
                InlineKeyboardButton("বন্ধুদের সাথে", callback_data="type_friends"),
            ],
            [
                InlineKeyboardButton("Home", callback_data="nav_home"),
            ],
        ]
    )


def photo_mood_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("হাসিখুশি", callback_data="mood_happy"),
                InlineKeyboardButton("আবেগী", callback_data="mood_emotional"),
            ],
            [
                InlineKeyboardButton("ভ্রমণ বা আউটডোর", callback_data="mood_travel"),
            ],
            [
                InlineKeyboardButton("ধরন বদলাও", callback_data="nav_change_type"),
                InlineKeyboardButton("Home", callback_data="nav_home"),
            ],
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ছবির টেক্সট আইডিয়া", callback_data="sec_text"),
                InlineKeyboardButton("ক্যাপশন ও হুক", callback_data="sec_caption"),
            ],
            [
                InlineKeyboardButton("কমেন্ট ও এংগেজমেন্ট", callback_data="sec_engage"),
                InlineKeyboardButton("রিপ্লাই টেমপ্লেট", callback_data="sec_replies"),
            ],
            [
                InlineKeyboardButton("হ্যাশট্যাগ ও সময়", callback_data="sec_tags"),
                InlineKeyboardButton("অ্যালগরিদম চেকলিস্ট", callback_data="sec_algo"),
            ],
            [
                InlineKeyboardButton("নতুন অপশন দাও", callback_data="reload_pack"),
            ],
            [
                InlineKeyboardButton("মুড বদলাও", callback_data="nav_change_mood"),
                InlineKeyboardButton("ধরন বদলাও", callback_data="nav_change_type"),
            ],
            [
                InlineKeyboardButton("Home", callback_data="nav_home"),
            ],
        ]
    )


def text_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ছবিতে টেক্সট বসাও", callback_data="render_overlay"),
            ],
            [
                InlineKeyboardButton("Back", callback_data="nav_back"),
                InlineKeyboardButton("Home", callback_data="nav_home"),
            ],
        ]
    )


def sub_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Back", callback_data="nav_back"),
                InlineKeyboardButton("Home", callback_data="nav_home"),
            ]
        ]
    )


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        os.getenv("FONT_PATH", "").strip(),
        os.path.join(BASE_DIR, "fonts", "NotoSansBengali-Regular.ttf"),
        os.path.join(BASE_DIR, "fonts", "HindSiliguri-Regular.ttf"),
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf",
        "/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf",
        "C:\\Windows\\Fonts\\Nirmala.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def get_overlay_text(photo_type: str, photo_mood: str, seed: int = 0) -> str:
    overlay_variants = {
        ("self", "happy"): [
            "নিজের মতো করে • আজকের ভালো থাকার ডোজ",
            "হালকা হাসি, সহজ দিন • নিজের ভাইবস",
        ],
        ("self", "emotional"): [
            "ছোট্ট একটা বিরতি • হৃদয়ের কাছের একটা ফ্রেম",
            "কিছু কথা না বলা থাক • মনের ভেতরের গল্প",
        ],
        ("self", "travel"): [
            "নতুন জায়গা, নতুন গল্প • খোলা হাওয়ায়",
            "একটু বেরিয়ে পড়া • পথের টানে",
        ],
        ("family", "happy"): [
            "সবচেয়ে বড় আশ্রয় • একসাথে হাসির মুহূর্ত",
            "আপনজনদের সাথে • আজকের সেরা সময়",
        ],
        ("family", "emotional"): [
            "আমাদের ছোট্ট পৃথিবী • চিরন্তন ভালোবাসা",
            "সবকিছুর ঊর্ধ্বে পরিবার • হৃদয়ের টান",
        ],
        ("family", "travel"): [
            "পরিবারের সাথে ভ্রমণ • সেরা সময়",
            "একসাথে নতুন পথ • পরিবারের আনন্দ",
        ],
        ("friends", "happy"): [
            "আড্ডার প্রমাণপত্র • প্ল্যান বদলাবেই",
            "এদের সাথে থাকলে গল্প জমে • ফুল এনার্জি",
        ],
        ("friends", "emotional"): [
            "সময় বদলায়, স্মৃতি নয় • কাছের মানুষ",
            "পুরনো দিনের ছোঁয়া • সেরা টিম",
        ],
        ("friends", "travel"): [
            "পথে কাটানো সেরা সময় • আউটডোর ভাইবস",
            "নতুন জায়গায় আড্ডা • ভ্রমণের স্মৃতি",
        ],
    }
    options = overlay_variants.get(
        (photo_type, photo_mood),
        ["আজকের বিশেষ মুহূর্ত", "সহজ দিনের সুন্দর ফ্রেম"],
    )
    return options[seed % len(options)]


def render_photo_with_text(image_bytes: bytes, overlay_text: str) -> io.BytesIO:
    with Image.open(io.BytesIO(image_bytes)) as img:
        base = img.convert("RGBA")
        width, height = base.size

        banner_height = max(70, int(height * 0.14))
        banner_top = height - banner_height

        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        draw.rectangle(
            [(0, banner_top), (width, height)],
            fill=(15, 20, 30, 185),
        )

        font_size = max(18, min(44, int(banner_height * 0.36)))
        font = load_font(font_size)

        bbox = draw.textbbox((0, 0), overlay_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        while text_w > width * 0.9 and font_size > 14:
            font_size -= 2
            font = load_font(font_size)
            bbox = draw.textbbox((0, 0), overlay_text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

        text_x = (width - text_w) / 2 - bbox[0]
        text_y = banner_top + (banner_height - text_h) / 2 - bbox[1]

        draw.text(
            (text_x + 2, text_y + 2),
            overlay_text,
            font=font,
            fill=(0, 0, 0, 210),
        )
        draw.text(
            (text_x, text_y),
            overlay_text,
            font=font,
            fill=(255, 255, 255, 255),
        )

        combined = Image.alpha_composite(base, overlay).convert("RGB")
        output = io.BytesIO()
        output.name = "fb_ready_photo.jpg"
        combined.save(output, format="JPEG", quality=92)
        output.seek(0)
        return output


def build_algo_pack(
    photo_type: str,
    type_label: str,
    photo_mood: str,
    mood_label: str,
    file_id: str,
    seed: int = 0,
) -> dict[str, str]:
    short_tag = file_id[-6:] if file_id else "post"
    suggested_line = get_overlay_text(photo_type, photo_mood, seed)
    variant_no = seed + 1

    caption_sets = {
        "happy": [
            (
                f"১. হুক + মজা ({type_label}): 'সব টেনশন একপাশে রেখে আজ শুধু হাসি আর ভালো মুহূর্তগুলো উপভোগ করা!'\n"
                "২. শর্ট ও স্ক্রল-স্টপার: 'দিনটা কেমন যাবে ঠিক নেই, কিন্তু এই মুহূর্তটা দারুণ ছিল।'\n"
                "৩. শেয়ার-ফ্রেন্ডলি: 'ছবিটা দেখেই যদি ঠোঁটে হালকা হাসি আসে, তবে দিনটা সার্থক!'"
            ),
            (
                f"১. হালকা ভাইব ({type_label}): 'ভালো থাকার জন্য খুব বড় কিছুর দরকার হয় না, একটু হাসি আর ভালো সময়ই যথেষ্ট।'\n"
                "২. প্রশ্ন-হুক: 'এই ফ্রেমটা দেখে তোমার কোন স্মৃতিটা মনে পড়লো?'\n"
                "৩. সংক্ষিপ্ত লাইন: 'আজকের দিনটার সবচেয়ে ঝকঝকে অংশ।'"
            ),
        ],
        "emotional": [
            (
                f"১. হুক + আন্তরিকতা ({type_label}): 'সব কথা মুখে বলা হয় না, কিছু অনুভূতি এভাবেই ফ্রেমে জমে থাকে।'\n"
                "২. স্মৃতি ও রিলেট: 'সময় গড়িয়ে গেলেও কাছের মানুষেরা আর অনুভূতিগুলো মনে থেকে যায়।'\n"
                "৩. শান্ত হুক: 'এই ছবিটা দেখলে মনের ভেতর একটা চেনা নরম অনুভূতি কাজ করে।'"
            ),
            (
                f"১. গভীর ও সহজ ({type_label}): 'কিছু মুহূর্ত সময়ের সাথে পুরনো হয় না, বরং আরও আপন হয়ে ওঠে।'\n"
                "২. অনুভূতির লাইন: 'নিঃশব্দে অনেক কিছু বলে দেওয়ার মতো কয়েকটা ছবির একটি।'\n"
                "৩. শেষ হুক: 'তোমাদের জীবনেও কি এমন কোনো ফ্রেম আছে?'"
            ),
        ],
        "travel": [
            (
                f"১. হুক + ভ্রমণ ({type_label}): 'একটু বেরিয়ে পড়া মানেই মনটাকে নতুন করে ঝালিয়ে নেওয়া।'\n"
                "২. গল্পের ছোঁয়া: 'গন্তব্যের চেয়েও পথে কাটানো সময়টাই বেশি মনে থাকে!'\n"
                "৩. অ্যাকশন হুক: 'এমন একটা জায়গায় যাওয়ার প্ল্যান কবে করছো?'"
            ),
            (
                f"১. আউটডোর ফিল ({type_label}): 'চার দেয়ালের বাইরে গিয়ে একটু খোলা বাতাস নেওয়ার মজাই আলাদা।'\n"
                "২. হালকা লাইন: 'ছোট্ট ট্রিপ, কিন্তু মনটা অনেকটাই হালকা হয়ে গেল।'\n"
                "৩. প্রশ্ন-হুক: 'পরেরবার কোথায় ঘুরতে গেলে সবচেয়ে ভালো হবে?'"
            ),
        ],
    }

    engage_sets = {
        "happy": [
            (
                "• আলোচনা শুরু করার প্রশ্ন: 'সবচেয়ে বেশি হাসির ঘটনা কোনটা ছিল?'\n"
                "• দ্রুত কমেন্টের প্রশ্ন: '১ থেকে ১০-এ এই ভাইবটাকে কত দেবে?'\n"
                "• প্রথম কমেন্টে পিন করার মতো লাইন: 'ক্যামেরার ঠিক পেছনেই আসল মজাটা হচ্ছিল—বাকিটা বললে গল্প লম্বা হবে!'"
            ),
            (
                "• রিলেট করার প্রশ্ন: 'তোমার সপ্তাহের সেরা মুড কোন দিনটা থাকে?'\n"
                "• ছোট সাড়ার প্রশ্ন: 'এই ছবি দেখে এক শব্দে কী বলবে?'\n"
                "• প্রথম কমেন্টে পিন করার মতো লাইন: 'সবচেয়ে মজার অংশটা কিন্তু শেষের কয়েক সেকেন্ডে ছিল!'"
            ),
        ],
        "emotional": [
            (
                "• আলোচনা শুরু করার প্রশ্ন: 'তোমাদের জীবনে এমন কোন স্মৃতি আছে, যা দেখলে মন ছুঁয়ে যায়?'\n"
                "• সহজ উত্তরের প্রশ্ন: 'এক কথায় এই অনুভূতিটাকে কী বলবে?'\n"
                "• প্রথম কমেন্টে পিন করার মতো লাইন: 'কিছু ছবি শুধু দেখার জন্য নয়, চুপচাপ অনুভব করার জন্য।'"
            ),
            (
                "• স্মৃতির প্রশ্ন: 'নিজের কাছের কারও সাথে এমন কোনো মুহূর্ত শেয়ার করতে ইচ্ছে করছে?'\n"
                "• সহজ উত্তরের প্রশ্ন: 'এই ছবিটা দেখলে কোন গান বা লাইনটা মনে পড়ে?'\n"
                "• প্রথম কমেন্টে পিন করার মতো লাইন: 'সময়ের ভেতর লুকিয়ে থাকা ছোট্ট এক টুকরো শান্তি।'"
            ),
        ],
        "travel": [
            (
                "• আলোচনা শুরু করার প্রশ্ন: 'তোমার পরবর্তী ঘুরতে যাওয়ার প্ল্যান কোথায়?'\n"
                "• সহজ উত্তরের প্রশ্ন: 'ইনডোর নাকি আউটডোর—কোনটা বেশি ভালো লাগে?'\n"
                "• প্রথম কমেন্টে পিন করার মতো লাইন: 'এই জায়গাটার আসল রূপ চোখে না দেখলে বিশ্বাস হবে না!'"
            ),
            (
                "• পছন্দের প্রশ্ন: 'পাহাড়, নদী নাকি শহরের নতুন কোনো জায়গা—কোনটা টানে?'\n"
                "• ছোট সাড়ার প্রশ্ন: 'সর্বশেষ কবে ঘুরতে গিয়েছিলে?'\n"
                "• প্রথম কমেন্টে পিন করার মতো লাইন: 'পথে নামলেই অর্ধেক ক্লান্তি দূর হয়ে যায়।'"
            ),
        ],
    }

    reply_templates = (
        f"কমেন্ট রিপ্লাই টেমপ্লেট ({type_label} • {mood_label}):\n\n"
        "• মজার কমেন্টের রিপ্লাই: 'ধরতে পেরেছো তো! আসল ঘটনাটা শুনলে আরও হাসতে।'\n"
        "• প্রশংসা বা আন্তরিক কমেন্টের রিপ্লাই: 'অনেক ধন্যবাদ! তোমার কথাটা সত্যিই ভালো লাগলো।'\n"
        "• শুধু ইমোজি বা এক শব্দের কমেন্টে পাল্টা প্রশ্ন: 'এটা দেখে তোমার কোন কথাটা সবার আগে মনে পড়লো?'\n\n"
        "টিপস: পোস্ট করার পর প্রথম ৩০–৬০ মিনিটে রিপ্লাই দিলে আলোচনা বাড়ে এবং পোস্ট ফিডে সক্রিয় থাকে।"
    )

    tag_sets = {
        "happy": ["#HappyVibes #GoodMood #BanglaPost", "#SmileMore #DailyVibes #BanglaCaption"],
        "emotional": ["#Heartfelt #Memories #BanglaCaption", "#DeepVibes #LifeMoments #BanglaPost"],
        "travel": ["#TravelDiaries #OutdoorVibes #ExploreMore", "#WeekendTrip #NatureVibes #BanglaTravel"],
    }

    cap_list = caption_sets.get(photo_mood, caption_sets["happy"])
    eng_list = engage_sets.get(photo_mood, engage_sets["happy"])
    tag_list = tag_sets.get(photo_mood, ["#BanglaPost", "#DailyVibes"])

    return {
        "text_ideas": (
            f"ভিজ্যুয়াল টেক্সট আইডিয়া (সেট #{variant_no} • {type_label} • {mood_label}):\n\n"
            f"• প্রস্তাবিত ব্যানার লাইন: '{suggested_line}'\n"
            "• অ্যালগরিদম নোট: প্রথম ১.৫ সেকেন্ডে চোখ আটকাতে ছবির নিচের অংশে সংক্ষিপ্ত ও স্পষ্ট টেক্সট রাখো, মুখ বা মূল ফোকাস না ঢেকে।\n\n"
            "নিচের 'ছবিতে টেক্সট বসাও' বাটনে চাপলে ছবিতে এই টেক্সট বসিয়ে ফিরিয়ে দেব।"
        ),
        "captions": (
            f"ক্যাপশন ও হুক (সেট #{variant_no} • {type_label} • {mood_label}):\n\n"
            f"{cap_list[seed % len(cap_list)]}\n\n"
            "টিপস: প্রথম ২ লাইনেই কৌতূহল তৈরি করো এবং প্যারাগ্রাফে ফাঁকা রাখো যেন মোবাইলে পড়তে সহজ হয়।"
        ),
        "engagement": (
            f"কমেন্ট থ্রেড ও এংগেজমেন্ট (সেট #{variant_no} • {type_label} • {mood_label}):\n\n"
            f"{eng_list[seed % len(eng_list)]}\n\n"
            "অ্যালগরিদম নিয়ম: পোস্ট করার পর প্রথম ৩০–৬০ মিনিটে আসা প্রতিটি কমেন্টের পাল্টা রিপ্লাই দাও।"
        ),
        "replies": reply_templates,
        "tags_and_tips": (
            f"হ্যাশট্যাগ ও পোস্ট টাইমিং (সেট #{variant_no} • #{short_tag}):\n\n"
            f"• হ্যাশট্যাগ (সর্বোচ্চ ৩টি): {tag_list[seed % len(tag_list)]}\n"
            "• সেরা সময়: সন্ধ্যা ৬টা থেকে রাত ১০টার মধ্যে পোস্ট করলে অ্যাক্টিভ ইউজার বেশি পাওয়ার সম্ভাবনা থাকে।\n"
            "• লিংক সতর্কতা: মূল পোস্টের ভেতর বাইরের লিংক দিলে রিচ কমতে পারে; প্রয়োজন হলে প্রথম কমেন্টে দাও।"
        ),
        "algo_checklist": (
            f"ফেসবুক অ্যালগরিদম চেকলিস্ট ({type_label} • {mood_label}):\n\n"
            "১. হুক ও Dwell Time: শুরুতে আকর্ষণীয় লাইন ও পরিষ্কার ভিজ্যুয়াল আছে তো?\n"
            "২. অর্থবহ আলোচনা: শুধু 'Nice' নয়, বাস্তব কমেন্ট ও রিপ্লাই আসার মতো প্রশ্ন আছে তো?\n"
            "৩. শেয়ার/সেভ ভ্যালু: নিজের অনুভূতি বা বন্ধু/পরিবারের সাথে মেলার মতো জায়গা আছে তো?\n"
            "৪. স্প্যাম এড়ানো: অতিরিক্ত হ্যাশট্যাগ বা জোরপূর্বক এংগেজমেন্ট চাওয়া এড়ানো হয়েছে তো?\n"
            "৫. প্রথম ১ ঘণ্টা সক্রিয়তা: পোস্ট করার পরপরই কমেন্টের জবাব দেওয়ার জন্য প্রস্তুত তো?"
        ),
    }


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "স্বাগতম! ফেসবুকে পোস্ট করার জন্য একটি ছবি পাঠাও।\n"
        "ধরন ও মুড বেছে নিলে টেক্সট, ক্যাপশন, কমেন্ট হুক, রিপ্লাই টেমপ্লেট, হ্যাশট্যাগ ও রিলোড অপশন পাবে।"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = update.message.photo[-1]
    context.user_data.clear()
    context.user_data["photo_file_id"] = photo.file_id
    context.user_data["variation_seed"] = 0

    await update.message.reply_text(
        "ধাপ ১/২: ছবি পেয়েছি! এটি কোন ধরনের ছবি, বেছে নাও:",
        reply_markup=photo_type_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "nav_home":
        context.user_data.clear()
        await query.edit_message_text(
            "হোমে ফিরে এসেছি এবং আগের সব ডেটা ক্লিয়ার করা হয়েছে।\n"
            "নতুন একটি ছবি পাঠাও।"
        )
        return

    file_id = context.user_data.get("photo_file_id")
    if not file_id:
        await query.edit_message_text(
            "কোনো সক্রিয় ছবি পাওয়া যায়নি। আগে একটি ছবি পাঠাও।"
        )
        return

    if data in PHOTO_TYPES:
        type_label, type_key = PHOTO_TYPES[data]
        context.user_data["photo_type_label"] = type_label
        context.user_data["photo_type_key"] = type_key
        context.user_data["variation_seed"] = 0
        context.user_data.pop("photo_mood_label", None)
        context.user_data.pop("photo_mood_key", None)
        context.user_data.pop("pack", None)

        await query.edit_message_text(
            f"ধাপ ২/২: ধরন হিসেবে **{type_label}** বেছে নিয়েছো।\n"
            "এখন ছবির মুড বেছে নাও:",
            parse_mode="Markdown",
            reply_markup=photo_mood_keyboard(),
        )
        return

    if data in PHOTO_MOODS:
        type_label = context.user_data.get("photo_type_label")
        type_key = context.user_data.get("photo_type_key")
        if not type_label or not type_key:
            await query.edit_message_text(
                "আগে ছবির ধরন বেছে নাও:",
                reply_markup=photo_type_keyboard(),
            )
            return

        mood_label, mood_key = PHOTO_MOODS[data]
        seed = context.user_data.get("variation_seed", 0)
        context.user_data["photo_mood_label"] = mood_label
        context.user_data["photo_mood_key"] = mood_key
        context.user_data["pack"] = build_algo_pack(
            type_key, type_label, mood_key, mood_label, file_id, seed
        )

        await query.edit_message_text(
            f"নির্বাচিত: **{type_label}** • **{mood_label}** (সেট #{seed + 1})\n"
            "নিচের বাটনগুলো থেকে ধাপগুলো দেখে নাও:",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "nav_change_type":
        await query.edit_message_text(
            "ছবির নতুন ধরন বেছে নাও:",
            reply_markup=photo_type_keyboard(),
        )
        return

    if data == "nav_change_mood":
        type_label = context.user_data.get("photo_type_label", "নির্বাচিত ধরন")
        await query.edit_message_text(
            f"ধরন: **{type_label}**\nছবির নতুন মুড বেছে নাও:",
            parse_mode="Markdown",
            reply_markup=photo_mood_keyboard(),
        )
        return

    type_label = context.user_data.get("photo_type_label")
    type_key = context.user_data.get("photo_type_key")
    mood_label = context.user_data.get("photo_mood_label")
    mood_key = context.user_data.get("photo_mood_key")

    if not type_label or not type_key or not mood_label or not mood_key:
        await query.edit_message_text(
            "আগে ছবির ধরন ও মুড বেছে নাও:",
            reply_markup=photo_type_keyboard(),
        )
        return

    if data == "reload_pack":
        next_seed = context.user_data.get("variation_seed", 0) + 1
        context.user_data["variation_seed"] = next_seed
        context.user_data["pack"] = build_algo_pack(
            type_key, type_label, mood_key, mood_label, file_id, next_seed
        )
        await query.edit_message_text(
            f"নতুন অপশন তৈরি হয়েছে! (**{type_label}** • **{mood_label}** • সেট #{next_seed + 1})\n"
            "নিচের বাটনগুলো থেকে নতুন সেট দেখে নাও:",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    pack = context.user_data.get("pack")
    if not pack:
        seed = context.user_data.get("variation_seed", 0)
        pack = build_algo_pack(type_key, type_label, mood_key, mood_label, file_id, seed)
        context.user_data["pack"] = pack

    if data == "nav_back":
        seed = context.user_data.get("variation_seed", 
