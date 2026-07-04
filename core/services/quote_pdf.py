"""Quote PDF generation service.

Builds the freight quote PDF in-memory with ReportLab. Used by the
authenticated download endpoint (QuoteViewSet.generate_pdf) and the
quote-accepted confirmation email attachment.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def generate_quote_pdf_bytes(quote) -> bytes:
    """Generate the PDF quote document and return its raw bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=20*mm, leftMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)

    styles = getSampleStyleSheet()
    accent = colors.HexColor('#2563EB')
    dark = colors.HexColor('#0F172A')
    mid = colors.HexColor('#64748B')

    title_style = ParagraphStyle('title', fontSize=24, textColor=dark, spaceAfter=4, fontName='Helvetica-Bold')
    sub_style = ParagraphStyle('sub', fontSize=10, textColor=mid, spaceAfter=2)
    label_style = ParagraphStyle('label', fontSize=9, textColor=mid, fontName='Helvetica')
    value_style = ParagraphStyle('value', fontSize=10, textColor=dark, fontName='Helvetica-Bold')
    normal = styles['Normal']

    story = []

    # Header
    story.append(Paragraph('TRUCKWYS', title_style))
    story.append(Paragraph('Road Freight Intelligence Platform', sub_style))
    story.append(Spacer(1, 8*mm))

    # Quote title
    story.append(Paragraph(f'FREIGHT QUOTE', ParagraphStyle('qt', fontSize=16, textColor=accent, fontName='Helvetica-Bold', spaceAfter=2)))
    story.append(Paragraph(f'{quote.quote_number}', ParagraphStyle('qn', fontSize=12, textColor=mid, spaceAfter=6)))
    story.append(Spacer(1, 4*mm))

    # Quote meta table
    cname = quote.customer.name if quote.customer else 'Direct Customer'
    meta = [
        ['Customer', cname, 'Status', quote.status],
        ['Valid Until', str(quote.valid_until) if quote.valid_until else 'N/A', 'Created', str(quote.created_at.date())],
        ['Confidence', f'{quote.confidence or 0}%', 'Vehicle Type', quote.vehicle_type or 'Standard'],
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
        ['Delivery', quote.delivery_location or quote.destination or '—', 'SLA', f'{quote.sla_hours or 48}h'],
        ['Cargo', quote.cargo_description or '—', 'Weight', f'{quote.weight or 0} kg'],
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

    # Cost breakdown
    story.append(Paragraph('COST BREAKDOWN', ParagraphStyle('section', fontSize=10, textColor=mid, fontName='Helvetica-Bold', spaceAfter=3)))
    def zar(v):
        try: return f'R {float(v):,.2f}'
        except: return 'R 0.00'

    cost_data = [
        ['Description', 'Amount'],
        ['Base Rate', zar(quote.base_rate)],
        ['Fuel Surcharge', zar(quote.fuel_surcharge)],
        ['Toll Charges', zar(quote.toll_charges or 0)],
        ['Driver Allowance', zar(quote.driver_allowance or 0)],
        ['Additional Charges', zar(quote.additional_charges or 0)],
        ['TOTAL (excl. VAT)', zar(quote.total_amount)],
    ]
    cost_table = Table(cost_data, colWidths=[120*mm, 50*mm])
    cost_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), accent),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTNAME', (0,1), (-1,-2), 'Helvetica'),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#F1F5F9')),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('PADDING', (0,0), (-1,-1), 7),
    ]))
    story.append(cost_table)
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

    doc.build(story)
    buf.seek(0)
    return buf.read()
