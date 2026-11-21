"""Demo pictures for the Hjemmeside editor, used by ``manage.py seed_demo``.

The seeded pages are otherwise text only, so neither the picture layouts nor the image library had
anything to show. This adds a few library images and puts them in pages the way the editor does:

  * **Faciliteter** uses every placement (Venstre, Midt, Højre) and every size (Lille, Mellem, Stor,
    Fuld bredde), each as the editor's two classes on an <img> inside a paragraph
    (frontend/src/cms-image-layout.ts), so the editor and the preview both show each layout;
  * **Historie** has a picture the way the old site's pages have them, floated with an inline pixel
    size and no classes, because those keep their size until someone picks a layout, which then
    replaces the inline size;
  * one **news item** carries a picture too, since news is edited with the same editor.

The images go through cms.images.prepare_image like an upload, so they have the sha256, content type
and size a snapshot ships, and the library lists them as in use.
"""

import random
from datetime import datetime, timedelta
from io import BytesIO

from residents.models import Resident

from .images import prepare_image
from .models import CmsImage, NewsItem, Page
from .services import store_prepared_image

# name, caption (the default alt text), size in px, colour. One portrait, so a tall picture's layout
# is visible as well.
IMAGES = [
    ("facade", "Kollegiets facade", (1600, 900), "#6b4f3a"),
    ("spisesalen", "Spisesalen", (1200, 800), "#315b8d"),
    ("festsalen", "Festsalen pyntet til fest", (1200, 800), "#8a4e70"),
    ("musiklokalet", "Musiklokalet", (800, 1100), "#1f6c64"),
    ("gaarden", "Gården om sommeren", (1200, 800), "#517544"),
    ("kollegiet-1908", "Kollegiet i 1908", (900, 600), "#7a7062"),
    ("julefrokost", "Julefrokost i spisesalen", (1200, 800), "#9a3b2f"),
]

TEXT = (
    "Her er plads til fællesspisning, fester og møder, og rummet kan lånes af alle beboere. Book det "
    "hos inspektionen i god tid, og husk at rydde op bagefter, så det er klar til de næste."
)


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    """Create the library images and the pages that use them. Returns the number of images made."""
    uploader = residents[0] if residents else None
    images = {name: _store(name, caption, size, colour, uploader) for name, caption, size, colour in IMAGES}

    def img(name: str, layout: str = "", style: str = "") -> str:
        image = images[name]
        attributes = f' class="{layout}"' if layout else ""
        attributes += f' style="{style}"' if style else ""
        return f'<img src="{image.url}" alt="{image.caption}"{attributes}>'

    Page.objects.filter(slug="faciliteter").update(
        body=(
            f"<p>{img('facade', 'img-center img-full')}</p>"
            "<p>Kollegiet har fælles rum til det meste, og de kan bruges af alle beboere.</p>"
            "<h2>Spisesalen</h2>"
            f"<p>{img('spisesalen', 'img-left img-small')}{TEXT} {TEXT}</p>"
            "<h2>Festsalen</h2>"
            f"<p>{img('festsalen', 'img-right img-medium')}{TEXT} {TEXT}</p>"
            "<h2>Musiklokalet</h2>"
            f"<p>{img('musiklokalet', 'img-center img-medium')}</p>"
            "<p>Trommer, klaver og forstærkere. Øv, når du vil, men luk døren efter kl. 22.</p>"
            "<h2>Gården</h2>"
            f"<p>{img('gaarden', 'img-center img-large')}</p>"
            "<p>Grill, bænke og plads til en fodboldkamp.</p>"
        ),
        updated_at=now,
    )
    # The old site's markup, kept as it was imported (no layout classes).
    Page.objects.filter(slug="kollegielivet/historie").update(
        body=(
            "<p>"
            + img("kollegiet-1908", style="float: right; width: 300px; height: 200px; margin: 0 0 10px 10px")
            + "Kollegiet blev grundlagt i 1908 af G. A. Hagemann. Siden har generationer af studerende "
            "boet her, spist sammen og holdt fester i salen.</p>"
            "<p>Bygningen er den samme, men værelserne har fået nye køkkener og bad.</p>"
        ),
        updated_at=now,
    )
    NewsItem.objects.create(
        title="Julefrokosten blev en succes",
        body=(
            f"<p>{img('julefrokost', 'img-right img-small')}Tak til alle, der kom, og til køkkenet for "
            "maden. Billederne ligger i fotoalbummet.</p>"
        ),
        published_at=now - timedelta(days=9),
    )
    return len(images)


def _store(
    name: str, caption: str, size: tuple[int, int], colour: str, uploader: Resident | None
) -> CmsImage:
    from PIL import Image, ImageDraw

    width, height = size
    image = Image.new("RGB", size, colour)
    draw = ImageDraw.Draw(image)
    # A frame and a diagonal, so a cropped or stretched picture is obvious at a glance.
    draw.rectangle((0, 0, width - 1, height - 1), outline="white", width=8)
    draw.line((0, height, width, 0), fill="white", width=4)
    draw.text((40, 40), f"{caption} ({width}x{height})", fill="white", font_size=max(24, width // 24))
    output = BytesIO()
    image.save(output, format="JPEG", quality=82)
    return store_prepared_image(prepare_image(output), f"{name}.jpg", caption, uploader)
