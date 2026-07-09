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
    meta = [
        ['Customer', cname, 'Vehicle Type', quote.vehicle_type or 'Standard'],
        ['Collection Date', str(quote.pickup_date) if quote.pickup_date else 'To be confirmed',
         'Delivery Date', str(quote.delivery_date) if quote.delivery_date else 'To be confirmed'],
        ['Valid Until', str(quote.valid_until) if quote.valid_until else 'N/A', 'Quote Date', str(quote.created_at.date())],
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
    route_data = [
        ['Pickup', quote.pickup_location or quote.origin or '—', 'Distance', f'{quote.distance or 0} km'],
        ['Delivery', quote.delivery_location or quote.destination or '—', 'Weight', f'{quote.weight or 0} kg'],
        ['Cargo', quote.cargo_description or '—', 'Vehicle', quote.vehicle_type or 'Standard'],
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
    ]))
    story.append(route_table)
    story.append(Spacer(1, 6*mm))

    # Total price — the customer only ever sees the single all-in amount, never
    # the internal cost breakdown (fuel, tolls, driver allowance, margin).
    def zar(v):
        try: return f'R {float(v):,.2f}'
        except: return 'R 0.00'

    total_data = [
        ['TOTAL AMOUNT', zar(quote.total_amount)],
        ['Excl. VAT', ''],
    ]
    total_table = Table(total_data, colWidths=[110*mm, 60*mm])
    total_table.setStyle(TableStyle([
        # Soft tinted panel with accent rules top & bottom — cleaner than a solid bar
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#EFF4FF')),
        ('LINEABOVE', (0,0), (-1,0), 1.5, accent),
        ('LINEBELOW', (0,-1), (-1,-1), 1.5, accent),
        ('TEXTCOLOR', (0,0), (0,0), dark),
        ('TEXTCOLOR', (1,0), (1,0), accent),
        ('TEXTCOLOR', (0,1), (0,1), mid),
        ('FONTNAME', (0,0), (0,0), 'Helvetica-Bold'),
        ('FONTNAME', (1,0), (1,0), 'Helvetica-Bold'),
        ('FONTNAME', (0,1), (0,1), 'Helvetica'),
        ('FONTSIZE', (0,0), (0,0), 12),
        ('FONTSIZE', (1,0), (1,0), 18),
        ('FONTSIZE', (0,1), (0,1), 8),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,0), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 16),
        ('RIGHTPADDING', (0,0), (-1,-1), 16),
        ('TOPPADDING', (0,0), (-1,0), 14),
        ('BOTTOMPADDING', (0,0), (-1,0), 2),
        ('TOPPADDING', (0,1), (-1,1), 0),
        ('BOTTOMPADDING', (0,1), (-1,1), 12),
    ]))
    story.append(total_table)
    story.append(Spacer(1, 6*mm))

    # Notes
    if quote.notes:
        story.append(Paragraph('NOTES', ParagraphStyle('section', fontSize=10, textColor=mid, fontName='Helvetica-Bold', spaceAfter=3)))
        story.append(Paragraph(quote.notes, ParagraphStyle('notes', fontSize=9, textColor=dark, spaceAfter=4)))

    # T&C
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph('Terms & Conditions', ParagraphStyle('tc', fontSize=9, textColor=mid, fontName='Helvetica-Bold', spaceAfter=2)))
    story.append(Paragraph(
        'This quote is valid for the period indicated. Prices subject to fuel surcharge adjustments. '
        'Payment terms: 30 days from invoice date. All rates in South African Rand (ZAR) excl. VAT.',
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
