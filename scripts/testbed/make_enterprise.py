"""Builds a made-up company's files for trying Recall: "Acme Corp", with meeting recordings (a video with
slides, two audio recordings, in several voices), emails, contracts, legal documents, invoices, a scanned receipt, HR
documents and screenshots. Every fact is planted so a search has one right answer (see ANSWERS at the end).

    uv run --with piper-tts --with pillow scripts/testbed/make_enterprise.py ~/RecallTestbed/Acme --voices DIR

Needs ffmpeg and LibreOffice (soffice) on PATH, and Piper voices (en_US-lessac-medium, en_US-ryan-medium,
en_GB-alba-medium: .onnx + .onnx.json from huggingface.co/rhasspy/piper-voices) in --voices. Acme, Globex, CloudNova
and everyone in it are made up.
"""

import argparse
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def font(size, bold=False, mono=False):
    return ImageFont.truetype(MONO if mono else BOLD if bold else FONT, size)


def run(*command, cwd=None):
    subprocess.run(command, check=True, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# MARK: Speech

class Voices:
    def __init__(self, folder):
        from piper import PiperVoice
        self.voices = {name: PiperVoice.load(str(Path(folder) / f"{model}.onnx")) for name, model in
                       (("dana", "en_US-lessac-medium"), ("marc", "en_US-ryan-medium"), ("fiona", "en_GB-alba-medium"))}

    def say(self, speaker, text, out):
        with wave.open(str(out), "wb") as file:
            self.voices[speaker].synthesize_wav(text, file)

    def conversation(self, lines, out, work):
        """Lines spoken in turn, with a short pause between speakers, as one WAV. Returns each line's start time."""
        pieces, starts, t = [], [], 0.0
        silence = work / "pause.wav"
        run("ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "0.6", silence)
        for index, (speaker, text) in enumerate(lines):
            piece = work / f"line{index}.wav"
            self.say(speaker, text, piece)
            with wave.open(str(piece)) as file:
                seconds = file.getnframes() / file.getframerate()
            starts.append(t)
            pieces += [piece, silence]
            t += seconds + 0.6
        listing = work / "lines.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in pieces))
        run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing, "-ar", "22050", "-ac", "1", out)
        return starts, t


# MARK: Pictures

def slide(title, bullets, out, chart=None):
    image = Image.new("RGB", (1280, 720), "#F7F7F5")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 1280, 96], fill="#1F3A5F")
    draw.text((56, 26), title, font=font(40, bold=True), fill="white")
    draw.text((1080, 36), "ACME CORP", font=font(22, bold=True), fill="#9FB7D6")
    y = 150
    for bullet in bullets:
        draw.ellipse([60, y + 12, 74, y + 26], fill="#D97B1A")
        draw.text((92, y), bullet, font=font(30), fill="#222")
        y += 66
    if chart:
        # A bar chart: label → value, for "a chart of cloud costs".
        left, base, width = 700, 640, 110
        top = max(chart.values())
        for index, (label, value) in enumerate(chart.items()):
            x = left + index * (width + 30)
            height = int(380 * value / top)
            draw.rectangle([x, base - height, x + width, base], fill="#3B7DD8" if index < len(chart) - 1 else "#D9481A")
            draw.text((x + 10, base + 10), label, font=font(22), fill="#333")
            draw.text((x + 10, base - height - 34), f"${value}k", font=font(22, bold=True), fill="#333")
    image.save(out)


def screenshot_dashboard(out):
    image = Image.new("RGB", (1440, 900), "#0F1720")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 1440, 56], fill="#17212B")
    draw.text((24, 16), "Acme Ops · Production monitoring", font=font(22, bold=True), fill="#E6EDF3")
    draw.rectangle([40, 100, 1400, 260], fill="#3A1418", outline="#F85149", width=3)
    draw.text((70, 120), "CRITICAL  payments-api", font=font(30, bold=True), fill="#F85149")
    draw.text((70, 170), "Payment gateway timeout: HTTP 504 from checkout.stripe-proxy.acme.internal", font=font(26),
              fill="#F0F6FC")
    draw.text((70, 212), "Error rate 23.4% · since 14:02 UTC · on-call: Marc Dubois", font=font(22), fill="#C9D1D9")
    for index, (name, value, color) in enumerate((("Requests/s", "1,284", "#3FB950"), ("p99 latency", "8.2 s", "#F85149"),
                                                  ("Failed orders", "312", "#F85149"), ("Uptime 30d", "99.71%", "#D29922"))):
        x = 40 + index * 345
        draw.rectangle([x, 300, x + 320, 460], fill="#161B22", outline="#30363D", width=2)
        draw.text((x + 24, 320), name, font=font(22), fill="#8B949E")
        draw.text((x + 24, 370), value, font=font(48, bold=True), fill=color)
    draw.text((40, 520), "Recent events", font=font(24, bold=True), fill="#E6EDF3")
    for index, event in enumerate(("14:02  payments-api  upstream timeout after 30000 ms (attempt 3/3)",
                                   "14:03  orders-worker  retry queue above 5,000 messages",
                                   "14:05  pagerduty  incident INC-2291 opened, assigned to Marc Dubois")):
        draw.text((40, 570 + index * 44), event, font=font(22, mono=True), fill="#C9D1D9")
    image.save(out)


def screenshot_chat(out):
    image = Image.new("RGB", (1440, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 300, 900], fill="#3F0E40")
    draw.text((24, 24), "Acme Corp", font=font(26, bold=True), fill="white")
    for index, channel in enumerate(("# general", "# product", "# legal-ops", "# finance")):
        draw.text((24, 100 + index * 40), channel, font=font(22), fill="#CFC3CF" if index != 1 else "white")
    draw.text((330, 24), "# product", font=font(26, bold=True), fill="#1D1C1D")
    messages = (("Sofia Martin", "10:41", "Heads up: the Globex renewal call is moved to Thursday 3 pm."),
                ("Marc Dubois", "10:44", "Can we get the VAT checkout fix (ACME-1423) in before then?"),
                ("Dana Kim", "10:46", "Yes, I'll ship it by Friday. Launch date stays November 3rd."),
                ("Sofia Martin", "10:47", "Great. Also reminder: expense reports are due by the 25th."))
    for index, (who, when, text) in enumerate(messages):
        y = 100 + index * 110
        draw.rounded_rectangle([330, y, 382, y + 52], 8, fill=("#E8912D", "#2EB67D", "#36C5F0", "#E8912D")[index])
        draw.text((400, y), who, font=font(22, bold=True), fill="#1D1C1D")
        draw.text((400 + draw.textlength(who, font=font(22, bold=True)) + 12, y + 3), when, font=font(18), fill="#616061")
        draw.text((400, y + 34), text, font=font(22), fill="#1D1C1D")
    image.save(out)


def screenshot_ticket(out):
    image = Image.new("RGB", (1440, 900), "#F4F5F7")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 1440, 56], fill="#0052CC")
    draw.text((24, 14), "Acme Tracker", font=font(24, bold=True), fill="white")
    draw.text((60, 90), "ACME-1423", font=font(22), fill="#5E6C84")
    draw.text((60, 125), "Checkout fails when a company enters a VAT number", font=font(36, bold=True), fill="#172B4D")
    fields = (("Status", "In progress"), ("Priority", "Highest"), ("Assignee", "Dana Kim"), ("Reporter", "Marc Dubois"),
              ("Fix version", "2026.11 launch"), ("Customer", "Globex Industries"))
    for index, (name, value) in enumerate(fields):
        draw.text((1000, 200 + index * 50), name, font=font(20), fill="#5E6C84")
        draw.text((1160, 200 + index * 50), value, font=font(20, bold=True), fill="#172B4D")
    draw.text((60, 200), "Description", font=font(24, bold=True), fill="#172B4D")
    lines = ("EU business customers who type an intra-community VAT number get",
             "\"Error 422: tax_id invalid\" at the last step, and the order is lost.",
             "Started after the tax provider upgrade on September 30.",
             "Globex reported 41 failed orders this week.")
    for index, line in enumerate(lines):
        draw.text((60, 250 + index * 40), line, font=font(22), fill="#172B4D")
    image.save(out)


def org_chart(out):
    image = Image.new("RGB", (1400, 800), "white")
    draw = ImageDraw.Draw(image)

    def box(x, y, name, role):
        draw.rounded_rectangle([x, y, x + 260, y + 90], 12, outline="#1F3A5F", width=3, fill="#EEF3FA")
        draw.text((x + 16, y + 14), name, font=font(24, bold=True), fill="#1F3A5F")
        draw.text((x + 16, y + 52), role, font=font(19), fill="#333")
    draw.text((40, 30), "Acme Corp · Organization chart · October 2026", font=font(30, bold=True), fill="#1F3A5F")
    box(570, 110, "Sofia Martin", "Chief Executive Officer")
    people = (("Dana Kim", "VP Product"), ("Marc Dubois", "Head of Engineering"), ("Fiona Walsh", "General Counsel"),
              ("Raj Patel", "Chief Financial Officer"))
    for index, (name, role) in enumerate(people):
        x = 60 + index * 330
        draw.line([700, 200, 700, 260, x + 130, 260, x + 130, 320], fill="#1F3A5F", width=3)
        box(x, 320, name, role)
    image.save(out)


def scanned_receipt(out):
    """A receipt that's only a picture (a phone scan): its words are found through OCR alone."""
    image = Image.new("L", (1240, 1754), 236)
    draw = ImageDraw.Draw(image)
    draw.rectangle([300, 200, 940, 1350], fill=252)
    lines = [("LE BISTROT DU PORT", 34, True), ("12 quai de la Joliette, Marseille", 22, False), ("", 22, False),
             ("Date: 18/09/2026   Table 7", 24, False), ("4 x Menu du jour      96.00", 26, False),
             ("2 x Bouteille Bandol  58.00", 26, False), ("1 x Cafe gourmand     9.50", 26, False),
             ("", 22, False), ("TOTAL EUR            163.50", 30, True), ("TVA 10%               14.86", 24, False),
             ("", 22, False), ("Client dinner - Globex team", 24, False), ("Paid by card ****4471", 22, False)]
    y = 260
    for text, size, bold in lines:
        draw.text((340, y), text, font=font(size, bold=bold, mono=not bold), fill=40)
        y += size + 30
    image.rotate(1.2, fillcolor=236).save(out, "PDF", resolution=150)


# MARK: Documents

STYLE = ("<style>body{font-family:'DejaVu Serif';font-size:11pt;line-height:1.45} h1{font-size:18pt} "
         "h2{font-size:13pt;margin-top:18pt}</style>")


def document(html_body, out, formats, title):
    """Writes the HTML through LibreOffice as each of `formats` (pdf, docx, odt, rtf), named `out`.<format>."""
    work = Path(tempfile.mkdtemp())
    source = work / (out.stem + ".html")
    source.write_text(f"<html><head><meta charset='utf-8'><title>{title}</title>{STYLE}</head><body>{html_body}"
                      "</body></html>")
    for kind in formats:
        target = {"pdf": "pdf:writer_pdf_Export", "docx": "docx:MS Word 2007 XML", "odt": "odt",
                  "rtf": "rtf"}[kind]
        run("soffice", "--headless", "--convert-to", target, "--outdir", work, source)
        shutil.move(work / f"{out.stem}.{kind}", out.with_suffix(f".{kind}"))
    shutil.rmtree(work)


def paragraphs(*sections):
    out = []
    for heading, text in sections:
        out.append(f"<h2>{heading}</h2>")
        out.extend(f"<p>{p}</p>" for p in text.split("\n\n"))
    return "".join(out)


FILLER = ("The parties shall cooperate in good faith and keep each other reasonably informed of any matter that may "
          "affect the performance of this Agreement. Notices shall be given in writing to the addresses set out "
          "above, or to any other address a party designates by notice. Headings are for convenience only and do "
          "not affect interpretation. If any provision is held invalid, the remainder of this Agreement continues "
          "in full force.")


def contracts(folder):
    msa = paragraphs(
        ("1. Services", "This Master Services Agreement is between Acme Corp (\"Provider\") and Globex Industries "
                        "SA (\"Client\"). Provider supplies the Acme Checkout platform, hosting and support "
                        "described in each Statement of Work.\n\n" + FILLER),
        ("2. Fees and payment", "Client pays the fees in each Statement of Work within forty-five (45) days of "
                                "invoice. Late amounts bear interest at 1.5% per month.\n\n" + FILLER),
        ("3. Service levels", "Provider targets 99.9% monthly availability. If availability falls below 99.5%, "
                              "Client receives a service credit of 10% of that month's fees.\n\n" + FILLER),
        ("4. Term and termination", "This Agreement runs for an initial term of three (3) years from 1 January "
                                    "2026 and renews for one-year periods. Either party may terminate this Agreement "
                                    "for convenience with ninety (90) days' written notice. Either party may "
                                    "terminate immediately if the other commits a material breach that remains "
                                    "uncured thirty (30) days after notice.\n\n" + FILLER),
        ("5. Limitation of liability", "Except for breaches of confidentiality, each party's total liability under "
                                       "this Agreement is capped at the fees paid in the twelve (12) months before "
                                       "the claim. Neither party is liable for lost profits.\n\n" + FILLER),
        ("6. Governing law", "This Agreement is governed by French law. The courts of Lyon have exclusive "
                             "jurisdiction.\n\n" + FILLER))
    document("<h1>Master Services Agreement — Acme Corp and Globex Industries</h1>"
             "<p>Signed in Lyon on 15 December 2025.</p>" + msa,
             folder / "Globex Master Services Agreement 2026", ["pdf", "docx"], "Globex MSA")
    nda = paragraphs(
        ("1. Confidential information", "Each party may disclose business, technical and financial information to "
                                        "the other in connection with a possible acquisition of Initech Labs. The "
                                        "receiving party keeps it confidential and uses it only to evaluate that "
                                        "transaction.\n\n" + FILLER),
        ("2. Duration", "The obligations in this mutual non-disclosure agreement last five (5) years from the date "
                        "of signature.\n\n" + FILLER),
        ("3. Return of information", "On request, each party returns or destroys the other's confidential "
                                     "information within fifteen (15) days.\n\n" + FILLER))
    document("<h1>Mutual Non-Disclosure Agreement</h1><p>Between Acme Corp and Initech Labs, 2 September 2026.</p>"
             + nda, folder / "NDA Initech Labs (mutual)", ["pdf"], "NDA Initech")
    lease = paragraphs(
        ("1. Premises", "Landlord leases to Acme Corp the third floor of 40 rue de la République, 69002 Lyon, about "
                        "820 square meters, for use as offices.\n\n" + FILLER),
        ("2. Rent", "Annual rent is EUR 246,000 excluding charges, payable quarterly in advance. Rent is indexed "
                    "every year on the ILAT index.\n\n" + FILLER),
        ("3. Term and early termination", "The lease runs nine years from 1 March 2027. The tenant may leave at the "
                                          "end of each three-year period with six (6) months' notice by registered "
                                          "letter.\n\n" + FILLER),
        ("4. Works", "The tenant may not make structural works without the landlord's prior written consent. The "
                     "landlord delivers the premises with fibre internet and air conditioning.\n\n" + FILLER))
    document("<h1>Commercial Lease — Lyon Office</h1>" + lease, folder / "Lyon office lease", ["pdf", "odt"],
             "Lyon lease")
    employment = paragraphs(
        ("1. Position", "Acme Corp employs Léa Moreau as Senior Data Engineer from 4 January 2027, reporting to Marc "
                        "Dubois.\n\n" + FILLER),
        ("2. Pay", "Gross annual salary EUR 72,000, paid monthly, with a target bonus of 10%.\n\n" + FILLER),
        ("3. Non-compete", "For twelve (12) months after leaving, the employee shall not work for a direct "
                           "competitor of Acme Corp in France, in return for monthly compensation equal to 50% of "
                           "her average monthly salary.\n\n" + FILLER),
        ("4. Probation", "The first four months are a probation period, renewable once.\n\n" + FILLER))
    document("<h1>Employment Contract</h1>" + employment, folder / "Employment contract - Lea Moreau", ["docx"],
             "Employment contract")


def legal(folder):
    dpa = paragraphs(
        ("1. Subject", "This Data Processing Agreement sets out how CloudNova SAS processes personal data on behalf "
                       "of Acme Corp, under Article 28 of the GDPR.\n\n" + FILLER),
        ("2. Security", "The processor encrypts personal data at rest and in transit, and keeps backups in the "
                        "European Union only.\n\n" + FILLER),
        ("3. Personal data breach", "The processor notifies Acme Corp of any personal data breach without undue "
                                    "delay and in any event within twenty-four (24) hours of becoming aware of it, "
                                    "so that Acme Corp can notify the supervisory authority within 72 hours.\n\n"
                                    + FILLER),
        ("4. Sub-processors", "The processor may not engage another processor without Acme Corp's prior written "
                              "authorisation.\n\n" + FILLER))
    document("<h1>Data Processing Agreement — CloudNova</h1>" + dpa, folder / "DPA CloudNova (GDPR)", ["pdf"], "DPA")
    notice = ("<h1>Formal notice — trademark infringement</h1><p>Lyon, 29 September 2026</p>"
              "<p>To: Acmee Supplies Ltd, Manchester</p>"
              "<p>Dear Sirs,</p><p>We act for Acme Corp, owner of the European Union trademark ACME CHECKOUT "
              "(registration no. 018765432). Your online shop sells payment terminals under the sign \"Acmee "
              "Checkout\", which is likely to confuse customers.</p><p>We ask you to stop all use of the sign within "
              "thirty days of this letter, and to confirm it in writing. Failing that, Acme Corp will bring "
              "proceedings without further notice.</p><p>Yours faithfully,</p><p>Fiona Walsh, General Counsel</p>")
    document(notice, folder / "Trademark notice to Acmee Supplies", ["docx"], "Trademark notice")
    resolution = ("<h1>Board resolution</h1><p>Acme Corp board of directors, meeting of 6 October 2026.</p>"
                  "<p>The board approves the acquisition of 100% of Initech Labs for a price of EUR 4.2 million, "
                  "subject to due diligence, and authorises the CEO, Sofia Martin, to sign the share purchase "
                  "agreement.</p><p>The board approves opening the Lyon office in March 2027.</p>")
    document(resolution, folder / "Board resolution - Initech acquisition", ["odt"], "Board resolution")
    privacy = ("# Acme Checkout privacy policy\n\nWe collect the name, email address and billing address of people "
               "who buy through Acme Checkout, to process payments and prevent fraud.\n\n## How long we keep data\n\n"
               "Order records are kept for ten years, as French accounting law requires. Account data is deleted "
               "three years after the last purchase.\n\n## Your rights\n\nYou can ask for a copy of your data, or for "
               "its deletion, by writing to privacy@acme.example.\n")
    (folder / "Privacy policy.md").write_text(privacy)


def emails(folder):
    messages = [
        ("2026-09-22 Overdue invoice INV-2026-0187.html", "accounts@cloudnova.example", "Raj Patel",
         "Overdue invoice INV-2026-0187",
         "Dear Raj,<br><br>Our records show that invoice INV-2026-0187 for USD 48,200.00 (cloud hosting, August "
         "2026) is now 21 days overdue. Please arrange payment by bank transfer to the account on the invoice, or "
         "let us know if anything is disputed.<br><br>Best regards,<br>CloudNova Accounts Receivable"),
        ("2026-09-02 NDA for the Initech talks.html", "fiona.walsh@acme.example", "Sofia Martin",
         "NDA for the Initech talks",
         "Sofia,<br><br>Attached is the mutual NDA for the Initech Labs discussions. Please don't share any "
         "numbers with their team until both sides have signed. I've kept the term at five years.<br><br>Fiona"),
        ("2026-09-15 Postmortem payment outage.html", "marc.dubois@acme.example", "engineering@acme.example",
         "Postmortem: payment outage of September 14",
         "Team,<br><br>Summary: from 14:02 to 15:10 UTC, 23% of checkout payments failed with gateway timeouts. "
         "Root cause: an expired TLS certificate on the internal stripe proxy after the tax provider upgrade. "
         "312 orders failed; all customers were contacted.<br><br>Actions: automate certificate renewal (owner: "
         "Marc), add an alert 14 days before expiry, and retry failed orders automatically.<br><br>Marc"),
        ("2026-10-01 Offer letter Lea Moreau.html", "hr@acme.example", "lea.moreau@mail.example",
         "Your offer from Acme Corp",
         "Dear Léa,<br><br>We are delighted to offer you the position of Senior Data Engineer, starting 4 January "
         "2027, with a gross annual salary of EUR 72,000 and a 10% target bonus. Please reply by October 15 to "
         "accept.<br><br>Welcome aboard,<br>Acme Corp People Team"),
        ("2026-10-03 Quote for new laptops.html", "sales@laptopsdirect.example", "Raj Patel",
         "Quote Q-5521: 25 laptops",
         "Hello Raj,<br><br>As discussed, our quote for 25 ThinkPad T14 laptops with three-year on-site warranty "
         "comes to EUR 31,875 before VAT, delivery in two weeks. The quote is valid until October 31.<br><br>"
         "Kind regards,<br>Laptops Direct"),
        ("2026-10-07 Security warning - fake invoice email.html", "security@acme.example", "all@acme.example",
         "Warning: fake invoice emails",
         "Hi all,<br><br>We are seeing phishing emails pretending to be from CloudNova, asking to update bank "
         "details for invoice payments. Never change a supplier's bank details from an email; call the supplier "
         "on a known number first, and forward anything suspicious to security@acme.example.<br><br>Security team"),
    ]
    for name, sender, recipient, subject, body in messages:
        date = name[:10]
        (folder / name).write_text(
            f"<html><head><meta charset='utf-8'><title>{subject}</title></head><body>"
            f"<p><b>From:</b> {sender}<br><b>To:</b> {recipient}<br><b>Date:</b> {date}<br>"
            f"<b>Subject:</b> {subject}</p><hr><p>{body}</p></body></html>")


def finance(folder):
    def invoice(number, supplier, lines, total, due, name):
        rows = "".join(f"<tr><td>{d}</td><td align='right'>{a}</td></tr>" for d, a in lines)
        document(f"<h1>INVOICE {number}</h1><p><b>{supplier}</b><br>Bill to: Acme Corp, 12 avenue Foch, 75116 Paris"
                 f"</p><table width='100%' border='1' cellpadding='6'><tr><th>Description</th><th>Amount</th></tr>"
                 f"{rows}<tr><td><b>Total due</b></td><td align='right'><b>{total}</b></td></tr></table>"
                 f"<p>Payment due by {due}. Bank transfer to IBAN FR76 3000 4000 5000 6000 7000 891.</p>",
                 folder / name, ["pdf"], number)
    invoice("INV-2026-0187", "CloudNova SAS — cloud hosting", [("Compute, August 2026", "USD 31,400.00"),
                                                                ("Storage and backups", "USD 9,800.00"),
                                                                ("Premium support", "USD 7,000.00")],
            "USD 48,200.00", "1 September 2026", "CloudNova invoice INV-2026-0187")
    invoice("LX-88412", "Lexis Legal Services — outside counsel", [("Initech due diligence, 32 hours", "EUR 12,800.00"),
                                                                   ("Trademark notice drafting", "EUR 1,450.00")],
            "EUR 14,250.00", "30 October 2026", "Legal fees October")
    scanned_receipt(folder / "Scan 2026-09-18 restaurant receipt.pdf")
    report = ("Q3 2026 finance summary\n\nRevenue for the quarter was EUR 6.8 million, up 14% on Q3 2025, driven by "
              "Globex and two new retail customers. Gross margin fell from 71% to 66% because cloud hosting costs "
              "rose 30% after the August traffic peak.\n\nOperating cash flow was EUR 0.9 million. Cash at the end "
              "of September: EUR 11.3 million, enough for the Initech acquisition without new debt.\n\nOutlook: "
              "marketing spend is cut by 12% in Q4 and hiring is frozen until January, as decided at the September "
              "budget review.\n")
    (folder / "Q3 2026 finance summary.txt").write_text(report)


def hr(folder):
    handbook = ("# Acme Corp employee handbook\n\n## Working hours\n\nCore hours are 10:00 to 16:00; the rest is up to "
                "you. Remote work is allowed up to three days a week.\n\n## Expenses\n\nSubmit expense reports by the "
                "25th of each month with a photo of every receipt. Client dinners need the names of the guests.\n\n"
                "## Equipment\n\nEvery employee gets a laptop, renewed every three years. Report a lost or stolen "
                "laptop to security@acme.example within one hour.\n")
    (folder / "Employee handbook.md").write_text(handbook)
    leave = ("Leave policy (updated October 2026)\n\nPaid vacation: 25 working days a year, plus 10 days of RTT.\n\n"
             "Parental leave: from January 2027 every new parent, whatever their gender, gets 16 weeks of fully paid "
             "parental leave, to take within the child's first year.\n\nSick leave: tell your manager before 10:00 "
             "and send the doctor's note within 48 hours.\n")
    (folder / "Leave policy.txt").write_text(leave)
    org_chart(folder / "Org chart October 2026.png")


# MARK: Meetings

def meetings(folder, voices):
    work = Path(tempfile.mkdtemp())
    # The budget review: a video of the shared slides, with the call's sound.
    script = [
        ("dana", "Okay, let's start the Q3 budget review. First slide is revenue. We closed the quarter at six point "
                 "eight million euros, fourteen percent up on last year."),
        ("marc", "Nice. Globex alone was almost a fifth of that."),
        ("dana", "Next slide: costs. This is the one that worries me. Cloud hosting went up thirty percent since "
                 "August, after the traffic peak. The red bar on the chart is September."),
        ("marc", "Most of that is the payment service scaling up during the outage. We can bring it down with "
                 "reserved instances."),
        ("dana", "Good. So, decisions. We cut the marketing budget by twelve percent for the fourth quarter, and we "
                 "freeze hiring until January, except the data engineer role that's already signed."),
        ("marc", "Agreed. And the Lyon office still opens in March?"),
        ("dana", "Yes, the lease is signed, March twenty twenty-seven. That's it for today, thanks everyone."),
    ]
    starts, total = voices.conversation(script, work / "budget.wav", work)
    slides = [("Q3 2026 budget review", ["Revenue, costs, decisions", "Dana Kim · Marc Dubois · 14 September"]),
              ("Revenue: EUR 6.8M (+14%)", ["Globex: 19% of revenue", "2 new retail customers", "Churn: 1.8%"]),
              ("Costs: cloud hosting +30%", ["Since the August traffic peak", "Gross margin 71% → 66%"],
               {"Jun": 41, "Jul": 44, "Aug": 52, "Sep": 57}),
              ("Decisions", ["Marketing budget −12% in Q4", "Hiring freeze until January", "Except the signed data "
                                                                                          "engineer", "Lyon office "
                                                                                                      "opens March "
                                                                                                      "2027"])]
    # Each slide shows from the line that brings it up.
    changes = [0.0, starts[0] + 4.5, starts[2], starts[4]]
    listing = []
    for index, item in enumerate(slides):
        path = work / f"slide{index}.png"
        slide(item[0], item[1], path, item[2] if len(item) > 2 else None)
        end = changes[index + 1] if index + 1 < len(changes) else total
        listing.append(f"file '{path}'\nduration {end - changes[index]:.2f}\n")
    listing.append(f"file '{work / f'slide{len(slides) - 1}.png'}'\n")
    (work / "slides.txt").write_text("".join(listing))
    run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", work / "slides.txt", "-i", work / "budget.wav",
        "-vf", "fps=10,format=yuv420p", "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "96k",
        "-shortest", folder / "2026-09-14 Q3 budget review (recording).mp4")

    # The product sync: sound only.
    sync = [
        ("marc", "Quick product sync. The big one: the launch date moves from October twentieth to November "
                 "third. We need two more weeks for the VAT checkout bug."),
        ("dana", "That's ticket fourteen twenty-three. Business customers who enter a VAT number get an error at "
                 "the last step, and the order is lost. Globex reported forty-one failed orders."),
        ("marc", "Then it's the top priority. Dana, can you ship the fix by Friday?"),
        ("dana", "Yes. And I'll tell Globex before the renewal call on Thursday, they were not happy."),
        ("marc", "Last thing: the new onboarding emails go out next week. Thanks."),
    ]
    voices.conversation(sync, work / "sync.wav", work)
    run("ffmpeg", "-y", "-i", work / "sync.wav", "-c:a", "aac", "-b:a", "96k",
        folder / "2026-09-21 Product sync.m4a")

    # The all-hands: the CEO, then questions.
    all_hands = [
        ("fiona", "Good morning everyone, and thanks for joining the October all-hands. Three announcements today."),
        ("fiona", "First, we are opening a second office in Lyon next March, on rue de la République. About thirty "
                  "people from engineering and support will be based there."),
        ("fiona", "Second, our new parental leave policy: from January, every new parent gets sixteen weeks of "
                  "fully paid leave, whatever their gender."),
        ("fiona", "Third, the board has approved the acquisition of Initech Labs. Their team of twelve joins us "
                  "once the deal closes, probably in December."),
        ("marc", "A question from the chat: will people have to move to Lyon?"),
        ("fiona", "No. Nobody has to move. The Lyon office is for new hires and anyone who wants to go."),
    ]
    voices.conversation(all_hands, work / "allhands.wav", work)
    run("ffmpeg", "-y", "-i", work / "allhands.wav", "-c:a", "libmp3lame", "-q:a", "5",
        folder / "2026-10-02 All-hands meeting.mp3")
    shutil.rmtree(work)


# What a search should find (query → file, and the page or second when it matters).
ANSWERS = """
the clause about ending the Globex contract early   Contracts/Globex Master Services Agreement 2026.pdf  (page 2)
when did they decide to freeze hiring               Meetings/2026-09-14 Q3 budget review (recording).mp4  (decisions)
a chart of rising cloud costs                       Meetings/…budget review….mp4  (the costs slide)
why was the launch date moved                       Meetings/2026-09-21 Product sync.m4a
parental leave                                      HR/Leave policy.txt, Meetings/2026-10-02 All-hands meeting.mp3
INV-2026-0187                                       Finance/CloudNova invoice…pdf, Emails/2026-09-22 Overdue invoice…
how fast must a data breach be reported             Legal/DPA CloudNova (GDPR).pdf
non-compete                                         Contracts/Employment contract - Lea Moreau.docx
payment gateway timeout screenshot                  Screenshots/…monitoring dashboard….png
dinner with Globex receipt                          Finance/Scan 2026-09-18 restaurant receipt.pdf  (OCR only)
who reports to the CEO                              HR/Org chart October 2026.png
phishing email about bank details                   Emails/2026-10-07 Security warning - fake invoice email.html
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--voices", type=Path, required=True)
    args = parser.parse_args()
    root = args.folder.expanduser()
    if root.exists():
        shutil.rmtree(root)
    parts = {name: root / name for name in ("Meetings", "Emails", "Contracts", "Legal", "Finance", "HR",
                                            "Screenshots")}
    for folder in parts.values():
        folder.mkdir(parents=True)
    print("documents…", flush=True)
    contracts(parts["Contracts"])
    legal(parts["Legal"])
    emails(parts["Emails"])
    finance(parts["Finance"])
    hr(parts["HR"])
    print("screenshots…", flush=True)
    screenshot_dashboard(parts["Screenshots"] / "Screenshot 2026-09-14 monitoring dashboard.png")
    screenshot_chat(parts["Screenshots"] / "Screenshot 2026-09-29 product channel.png")
    screenshot_ticket(parts["Screenshots"] / "Screenshot 2026-10-01 ACME-1423 ticket.png")
    print("meetings (speech)…", flush=True)
    meetings(parts["Meetings"], Voices(args.voices))
    count = sum(1 for _ in root.rglob("*") if _.is_file())
    print(f"{count} files in {root}\n\nWhat a search should find (kept out of the folder, which would match them all):"
          + ANSWERS)


if __name__ == "__main__":
    main()
