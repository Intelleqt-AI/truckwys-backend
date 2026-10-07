"""Quote PDF generation service.

Builds the freight quote PDF in-memory with ReportLab. Used by the
authenticated download endpoint (QuoteViewSet.generate_pdf) and the
quote-accepted confirmation email attachment.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from core.formatting import format_zar


# Words that name a truck or its body, never a cargo (mirrors the frontend's
# src/lib/cargo.ts so the PDF and the quote detail agree).
_TRUCK_WORDS = {
    'superlink', 'tautliner', 'tautliners', 'tri', 'axle', 'triaxle', 'rigid', 'interlink', 'semi', 'trailer',
    'truck', 'horse', 'flatbed', 'flat', 'deck', 'reefer', 'refrigerated', 'tanker', 'box', 'curtainsider',
    'curtain', 'side', 'sider', 'tipper', 'lowbed', 'ldv', 'light', 'medium', 'heavy', 'vehicle', 'tonnes',
    'tonne', 'ton', 't', 'x', '6x4', '4x2', '8x4', 'and',
}


def _only_truck_words(text):
    import re
    text = re.sub(r'\([^)]*\)', ' ', text.lower())
    text = re.sub(r'[&/–—-]', ' ', text)
    words = [w for w in re.split(r'[^a-z0-9x]+', text) if w and not w.isdigit()]
    return bool(words) and all(w in _TRUCK_WORDS for w in words)


def quote_cargo_text(cargo, vehicle_type=None):
    """The cargo as the operator described it, or None. The builder saves
    "<weight>t <vehicle type>" when Cargo is left blank; that names the
    truck, not the cargo. Never falls back to the vehicle type."""
    import re
    c = str(cargo or '').strip()
    if not c:
        return None
    m = re.match(r'^\d+(?:[.,]\d+)?\s*t(?:\s+(.*))?$', c, re.IGNORECASE)
    if m:
        rest = (m.group(1) or '').strip().lower()
        vt = str(vehicle_type or '').strip().lower()
        if not rest or (vt and rest == vt) or _only_truck_words(rest):
            return None
    return c[:1].upper() + c[1:]

def generate_quote_pdf_bytes(quote) -> bytes:
    """Generate the PDF quote document and return its raw bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=20*mm, leftMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)

    styles = getSampleStyleSheet()
    accent = colors.HexColor('#2563EB')
    dark = colors.HexColor('#0F172A')
    mid = colors.HexColor('#64748B')

    title_style = ParagraphStyle('title', fontSize=24, leading=30, textColor=dark, spaceAfter=6, fontName='Helvetica-Bold')
    sub_style = ParagraphStyle('sub', fontSize=10, leading=14, textColor=mid, spaceAfter=3)
    label_style = ParagraphStyle('label', fontSize=9, textColor=mid, fontName='Helvetica')
    value_style = ParagraphStyle('value', fontSize=10, textColor=dark, fontName='Helvetica-Bold')
    normal = styles['Normal']

    story = []

    # Header — company branding (logo + details) pulled from the company profile
    company = getattr(quote, 'company', None)
    company_name = (company.company_name if company and company.company_name else 'TRUCKWYS')

    # Company logo, if one has been uploaded
    logo_flowable = None
    if company and getattr(company, 'logo', None):
        try:
            logo_path = company.logo.path
            img = Image(logo_path)
            # Constrain to a sensible header size, preserving aspect ratio
            max_h = 20 * mm
            max_w = 55 * mm
            iw, ih = img.imageWidth, img.imageHeight
            if ih and iw:
                scale = min(max_w / iw, max_h / ih)
                img.drawWidth = iw * scale
                img.drawHeight = ih * scale
            logo_flowable = img
        except Exception:
            logo_flowable = None

    # Company detail lines (reg / vat / address / contact)
    detail_bits = []
    if company:
        if company.registration_number:
            detail_bits.append(f'Reg No: {company.registration_number}')
        if company.vat_number:
            detail_bits.append(f'VAT No: {company.vat_number}')
        addr = company.address if isinstance(company.address, dict) else {}
        addr_line = ', '.join(filter(None, [addr.get('street'), addr.get('city'), addr.get('postal_code')]))
        if addr_line:
            detail_bits.append(addr_line)
        contact = company.contact if isinstance(company.contact, dict) else {}
        contact_line = ' | '.join(filter(None, [
            f"Tel: {contact.get('phone')}" if contact.get('phone') else None,
            f"Email: {contact.get('email')}" if contact.get('email') else None,
        ]))
        if contact_line:
            detail_bits.append(contact_line)

    name_block = [Paragraph(company_name, title_style)]
    for bit in detail_bits:
        name_block.append(Paragraph(bit, sub_style))

    if logo_flowable is not None:
        header_table = Table([[logo_flowable, name_block]], colWidths=[60*mm, 110*mm])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        story.append(header_table)
    else:
        for f in name_block:
            story.append(f)
    story.append(Spacer(1, 8*mm))

    # Quote title
    story.append(Paragraph('FREIGHT QUOTE', ParagraphStyle('qt', fontSize=17, leading=22, textColor=accent, fontName='Helvetica-Bold', spaceAfter=7)))
    story.append(Paragraph(quote.quote_number, ParagraphStyle('qn', fontSize=11, leading=14, textColor=mid, fontName='Helvetica')))
    story.append(Spacer(1, 7*mm))

    # Quote meta table — customer-facing fields only (no internal status/confidence)
    cname = quote.customer.name if quote.customer else 'Direct Customer'
    # House date format ("13 Oct 2026"), as everywhere else in the app.
    def _d(value):
        if not value:
            return None
        return f'{value.day} {value:%b %Y}'
    meta = [
        ['Customer', cname, 'Vehicle Type', quote.vehicle_type or 'Standard'],
        ['Collection Date', _d(quote.pickup_date) or 'To be confirmed',
         'Delivery Date', _d(quote.delivery_date) or 'To be confirmed'],
        ['Valid Until', _d(quote.valid_until) or 'N/A', 'Quote Date', _d(quote.created_at.date())],
    ]
    meta_table = Table(meta, colWidths=[35*mm, 65*mm, 35*mm, 35*mm])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#F1F5F9')),
        ('BACKGROUND', (2,0), (2,-1), colors.HexColor('#F1F5F9')),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('TEXTCOLOR', (0,0), (0,-1), mid),
        ('TEXTCOLOR', (2,0), (2,-1), mid),
        ('FONTNAME', (1,0), (1,-1), 'Helvetica-Bold'),
        ('FONTNAME', (3,0), (3,-1), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('PADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 6*mm))

    # Route
    story.append(Paragraph('ROUTE', ParagraphStyle('section', fontSize=10, textColor=mid, fontName='Helvetica-Bold', spaceAfter=3)))
    # Values are Paragraphs so a long address wraps inside its cell instead of
    # running over the Distance/Weight columns; figures in en-ZA.
    from xml.sax.saxutils import escape
    from core.formatting import format_number
    cell = ParagraphStyle('routecell', fontName='Helvetica', fontSize=9, leading=11, textColor=dark)
    wrap = lambda text: Paragraph(escape(str(text)), cell)
    round_trip = getattr(quote, 'trip_type', '') == 'ROUND_TRIP'
    distance = f'{format_number(quote.distance, 1)} km' if quote.distance else '—'
    if round_trip and quote.distance:
        # Display only: the price covers both legs of a return trip.
        distance = f'{format_number(quote.distance, 1)} km each way (return trip)'
    weight = f'{format_number(quote.weight)} kg' if quote.weight else '—'
    cargo_text = quote_cargo_text(quote.cargo_description, quote.vehicle_type)
    cargo_cell_label = 'Cargo'
    vehicle_text = (f'{quote.vehicle.make} {quote.vehicle.model}'.strip() if getattr(quote, 'vehicle', None)
                    else 'To be assigned')
    route_data = [
        ['Pickup', wrap(quote.pickup_location or quote.origin or '—'), 'Distance', wrap(distance)],
        ['Delivery', wrap(quote.delivery_location or quote.destination or '—'), 'Weight', wrap(weight)],
        *([['Return', wrap(quote.return_location or quote.pickup_location or quote.origin or '—'),
            'Trip', wrap('Return trip')]] if round_trip else []),
        # The type is already in the meta table; this row names the actual
        # truck only once one is assigned (it used to repeat the type). The
        # cargo is the operator's own text (same rule as the app's cargo.ts):
        # the builder's "<weight>t <vehicle type>" placeholder is not cargo,
        # so with no real cargo the Cargo cell is left out entirely.
        ([cargo_cell_label, wrap(cargo_text), 'Vehicle', wrap(vehicle_text)] if cargo_text
         else ['Vehicle', wrap(vehicle_text), '', '']),
    ]
    route_table = Table(route_data, colWidths=[30*mm, 80*mm, 30*mm, 30*mm])
    route_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#F1F5F9')),
        ('BACKGROUND', (2,0), (2,-1), colors.HexColor('#F1F5F9')),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('TEXTCOLOR', (0,0), (0,-1), mid),
        ('TEXTCOLOR', (2,0), (2,-1), mid),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('PADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        # No cargo: the Vehicle value spans the row (no empty label cell).
        *([] if cargo_text else [('SPAN', (1, -1), (3, -1)), ('BACKGROUND', (2, -1), (2, -1), colors.white)]),
    ]))
    story.append(route_table)
    story.append(Spacer(1, 6*mm))

    # Total price — the customer only ever sees the single all-in amount, never
    # the internal cost breakdown (fuel, tolls, driver allowance, margin).
    def zar(v):
        try: return format_zar(v, minus='-')
        except: return format_zar(0, minus='-')

    # Price excl. VAT, the VAT on it, and the total incl. VAT (one source:
    # core.services.quote_vat, shared with the emails and the quote page).
    from core.services.quote_vat import quote_vat, vat_label
    v = quote_vat(quote)
    if v['vat_registered']:
        total_data = [
            ['Price excl. VAT', zar(v['subtotal'])],
            [vat_label(v), zar(v['vat'])],
            ['TOTAL INCL. VAT', zar(v['total'])],
        ]
    else:
        total_data = [['TOTAL AMOUNT', zar(v['total'])], ['No VAT charged', '']]
    last = len(total_data) - 1
    total_row = last if v['vat_registered'] else 0
    total_table = Table(total_data, colWidths=[110*mm, 60*mm])
    style = [
        # Soft tinted panel with accent rules top & bottom — cleaner than a solid bar
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#EFF4FF')),
        ('LINEABOVE', (0,0), (-1,0), 1.5, accent),
        ('LINEBELOW', (0,-1), (-1,-1), 1.5, accent),
        ('TEXTCOLOR', (0,0), (-1,-1), mid),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 16),
        ('RIGHTPADDING', (0,0), (-1,-1), 16),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,0), 12),
        ('BOTTOMPADDING', (0,-1), (-1,-1), 12),
        # The total: bold, larger, accent figure.
        ('TEXTCOLOR', (0,total_row), (0,total_row), dark),
        ('TEXTCOLOR', (1,total_row), (1,total_row), accent),
        ('FONTNAME', (0,total_row), (-1,total_row), 'Helvetica-Bold'),
        ('FONTSIZE', (0,total_row), (0,total_row), 12),
        ('FONTSIZE', (1,total_row), (1,total_row), 18),
    ]
    if v['vat_registered']:
        style.append(('LINEABOVE', (0,last), (-1,last), 0.5, colors.HexColor('#C7D2FE')))
        style.append(('TOPPADDING', (0,last), (-1,last), 10))
    else:
        style += [('FONTSIZE', (0,1), (0,1), 8), ('TOPPADDING', (0,1), (-1,1), 0)]
    total_table.setStyle(TableStyle(style))
    story.append(total_table)
    story.append(Spacer(1, 6*mm))

    # Notes
    if quote.notes:
        story.append(Paragraph('NOTES', ParagraphStyle('section', fontSize=10, textColor=mid, fontName='Helvetica-Bold', spaceAfter=3)))
        story.append(Paragraph(quote.notes, ParagraphStyle('notes', fontSize=9, textColor=dark, spaceAfter=4)))

    # QUOTE-RULES §11: one line naming the diesel price the quote was priced on.
    diesel_line = diesel_reference_line(quote)
    if diesel_line:
        story.append(Paragraph(diesel_line, ParagraphStyle('diesel', fontSize=8, textColor=mid, spaceAfter=2)))

    # T&C
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph('Terms & Conditions', ParagraphStyle('tc', fontSize=9, textColor=mid, fontName='Helvetica-Bold', spaceAfter=2)))
    story.append(Paragraph(
        'This quote is valid for the period indicated. Prices subject to fuel surcharge adjustments. '
        f'Payment terms: {_terms_days(quote)} days from invoice date. All amounts in South African Rand (ZAR).',
        ParagraphStyle('tcbody', fontSize=8, textColor=mid)
    ))

    # Footer — platform attribution
    story.append(Spacer(1, 6*mm))
    story.append(Paragraph(
        'Powered by TruckWys',
        ParagraphStyle('footer', fontSize=7, textColor=mid, alignment=1)
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()


def _terms_days(quote) -> int:
    """The customer's own payment terms in days (the same terms the invoice
    will use — core.services.invoice_lines.customer_terms), 30 by default."""
    try:
        from core.services.invoice_lines import customer_terms, terms_days_for
        return terms_days_for(customer_terms(quote.customer))
    except Exception:
        return 30


def diesel_reference_line(quote):
    """'Priced on diesel at R 32,80/L (official inland, 7 Oct 2026).' from the
    quote's pricing snapshot, or None when it has none (never a fallback)."""
    price = getattr(quote, 'fuel_price_used', None)
    source = getattr(quote, 'fuel_price_source', '') or ''
    if price is None or not source:
        return None
    from core.services.quote_costing import fmt_rand, sa_date
    zone = 'coastal' if (getattr(quote, 'fuel_zone', '') or '').upper() == 'COASTAL' else 'inland'
    what = {'official': f'official {zone}', 'own': 'own price', 'override': 'set for this quote'}.get(source, source)
    when = sa_date(getattr(quote, 'fuel_effective_from', None) if source == 'official' else None) \
        or sa_date(getattr(quote, 'priced_at', None))
    return f'Priced on diesel at {fmt_rand(float(price), 2)}/L ({what}' + (f', {when}' if when else '') + ').'
