"""
PDF Generator service for creating professional invoice PDFs.

Uses ReportLab to generate South African Tax Invoice PDFs compliant with
VAT requirements.
"""

from decimal import Decimal
from typing import Optional
from datetime import date
import os
from io import BytesIO

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

from core.models import Invoice, Company
from core.formatting import format_zar


class InvoicePDFGenerator:
    """Service for generating invoice PDFs."""

    def __init__(self, invoice: Invoice):
        """
        Initialize PDF generator.

        Args:
            invoice: Invoice to generate PDF for
        """
        self.invoice = invoice
        self.buffer = BytesIO()
        self.width, self.height = A4
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()

    def _setup_custom_styles(self) -> None:
        """Set up custom paragraph styles."""
        self.styles.add(ParagraphStyle(
            name='InvoiceTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#1e3a8a'),
            spaceAfter=12,
            alignment=TA_CENTER,
        ))

        self.styles.add(ParagraphStyle(
            name='CompanyName',
            parent=self.styles['Normal'],
            fontSize=16,
            textColor=colors.HexColor('#1e3a8a'),
            fontName='Helvetica-Bold',
            spaceAfter=6,
        ))

        self.styles.add(ParagraphStyle(
            name='SectionHeader',
            parent=self.styles['Normal'],
            fontSize=10,
            textColor=colors.HexColor('#64748b'),
            fontName='Helvetica-Bold',
            spaceAfter=4,
        ))

        self.styles.add(ParagraphStyle(
            name='RightAlign',
            parent=self.styles['Normal'],
            alignment=TA_RIGHT,
        ))

    def generate(self) -> str:
        """
        Generate invoice PDF and save to file.

        Returns:
            str: Path to the saved PDF file
        """
        # Create document
        doc = SimpleDocTemplate(
            self.buffer,
            pagesize=A4,
            rightMargin=20*mm,
            leftMargin=20*mm,
            topMargin=20*mm,
            bottomMargin=20*mm,
        )

        # Build content
        story = []
        story.extend(self._build_header())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_invoice_info())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_customer_info())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_line_items())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_totals())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_footer())

        # Build PDF
        doc.build(story)

        # Save to file
        return self._save_to_file()

    def _build_header(self) -> list:
        """Build PDF header with company logo and details."""
        elements = []

        # Use the invoice's own company (tenant) — never a global first()
        company = getattr(self.invoice, 'company', None)

        # Company logo, if one has been uploaded
        if company and getattr(company, 'logo', None):
            try:
                logo = Image(company.logo.path)
                max_h = 20 * mm
                max_w = 60 * mm
                iw, ih = logo.imageWidth, logo.imageHeight
                if iw and ih:
                    scale = min(max_w / iw, max_h / ih)
                    logo.drawWidth = iw * scale
                    logo.drawHeight = ih * scale
                logo.hAlign = 'LEFT'
                elements.append(logo)
                elements.append(Spacer(1, 3*mm))
            except Exception:
                pass

        # Company name
        company_name = company.company_name if company else "TruckWys"
        elements.append(Paragraph(company_name, self.styles['CompanyName']))

        # Company details
        if company:
            company_info = []
            if company.registration_number:
                company_info.append(f"Reg No: {company.registration_number}")
            if company.vat_number:
                company_info.append(f"VAT No: {company.vat_number}")

            if company_info:
                elements.append(Paragraph(" | ".join(company_info), self.styles['Normal']))

            # Address
            if company.address:
                address_lines = []
                if isinstance(company.address, dict):
                    if company.address.get('street'):
                        address_lines.append(company.address['street'])
                    city_parts = []
                    if company.address.get('city'):
                        city_parts.append(company.address['city'])
                    if company.address.get('postal_code'):
                        city_parts.append(company.address['postal_code'])
                    if city_parts:
                        address_lines.append(", ".join(city_parts))

                if address_lines:
                    elements.append(Paragraph("<br/>".join(address_lines), self.styles['Normal']))

            # Contact
            if company.contact:
                contact_info = []
                if isinstance(company.contact, dict):
                    if company.contact.get('phone'):
                        contact_info.append(f"Tel: {company.contact['phone']}")
                    if company.contact.get('email'):
                        contact_info.append(f"Email: {company.contact['email']}")

                if contact_info:
                    elements.append(Paragraph(" | ".join(contact_info), self.styles['Normal']))

        elements.append(Spacer(1, 5*mm))

        # TAX INVOICE title
        # Only a VAT vendor may issue a "tax invoice" (VAT Act s20).
        company = getattr(self.invoice, 'company', None)
        title = "TAX INVOICE" if getattr(company, 'vat_registered', True) else "INVOICE"
        elements.append(Paragraph(title, self.styles['InvoiceTitle']))

        return elements

    def _build_invoice_info(self) -> list:
        """Build invoice information section."""
        elements = []

        data = [
            ['Invoice Number:', self.invoice.invoice_number],
            ['Invoice Date:', self.invoice.issue_date.strftime('%d %B %Y')],
            ['Due Date:', self.invoice.due_date.strftime('%d %B %Y')],
            ['Payment Terms:', self.invoice.get_payment_terms_display()],
        ]

        table = Table(data, colWidths=[40*mm, 60*mm])
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#64748b')),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))

        elements.append(table)
        return elements

    def _build_customer_info(self) -> list:
        """Build customer information section."""
        elements = []

        elements.append(Paragraph("BILL TO:", self.styles['SectionHeader']))

        customer = self.invoice.customer
        customer_lines = [f"<b>{customer.name}</b>"]

        if hasattr(customer, 'vat_number') and customer.vat_number:
            customer_lines.append(f"VAT No: {customer.vat_number}")

        if customer.billing_address:
            customer_lines.append(customer.billing_address)
        elif customer.address:
            # Fall back to regular address if billing address not set
            address_parts = [customer.address, customer.city, customer.state, customer.zip_code]
            address_str = ", ".join(filter(None, address_parts))
            if address_str:
                customer_lines.append(address_str)

        if customer.email:
            customer_lines.append(f"Email: {customer.email}")

        if customer.phone:
            customer_lines.append(f"Phone: {customer.phone}")

        elements.append(Paragraph("<br/>".join(customer_lines), self.styles['Normal']))

        return elements

    def _build_line_items(self) -> list:
        """Build line items table."""
        elements = []

        elements.append(Paragraph("ITEMS:", self.styles['SectionHeader']))
        elements.append(Spacer(1, 3*mm))

        # Paragraph styles for wrapping cell content
        desc_style = ParagraphStyle(
            'ItemDesc',
            parent=self.styles['Normal'],
            fontSize=9,
            leading=12,
            wordWrap='LTR',
        )
        header_desc_style = ParagraphStyle(
            'ItemDescHeader',
            parent=self.styles['Normal'],
            fontSize=10,
            fontName='Helvetica-Bold',
            textColor=colors.white,
            leading=13,
        )

        # Header row — wrap header in Paragraph too so styles apply consistently
        data = [[Paragraph('Description', header_desc_style), 'Qty', 'Unit Price', 'Discount', 'VAT', 'Amount']]

        typed = list(self.invoice.lines.all()) if self.invoice.pk else []
        vat_label = {'STANDARD': None, 'ZERO_RATED': '0%', 'EXEMPT': 'Exempt', 'NO_VAT': '-'}
        for line in typed:
            qty = line.quantity.normalize()
            data.append([
                Paragraph(str(line.description), desc_style),
                f'{qty:f}',
                format_zar(line.unit_price, minus='-'),
                format_zar(line.discount_amount, minus='-') if line.discount_amount else '',
                vat_label.get(line.tax_code) or f'{line.tax_rate.normalize():f}%',
                format_zar(line.net_amount, minus='-'),
            ])

        # Pre-foundation invoices without typed lines: the old JSON, or one line.
        line_items = [] if typed else (self.invoice.line_items or [])

        def _num(v):
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0

        for item in line_items:
            data.append([
                Paragraph(str(item.get('description', '')), desc_style),
                str(item.get('quantity', 1)),
                format_zar(_num(item.get('unit_price')), minus='-'),
                '', '',
                format_zar(_num(item.get('amount')), minus='-'),
            ])

        if not typed and not line_items:
            if self.invoice.load:
                desc_text = f"Freight Charge - {self.invoice.load.pickup_location} → {self.invoice.load.delivery_location}"
            else:
                desc_text = "Freight Charge - Service"
            data.append([
                Paragraph(desc_text, desc_style),
                '1',
                format_zar(self.invoice.subtotal, minus='-'),
                '', '',
                format_zar(self.invoice.subtotal, minus='-'),
            ])

        table = Table(data, colWidths=[70*mm, 15*mm, 25*mm, 22*mm, 15*mm, 28*mm])
        table.setStyle(TableStyle([
            # Header styling
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a8a')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (-1, 0), 'RIGHT'),
            # Body styling
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ALIGN', (0, 1), (0, -1), 'LEFT'),
            ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
            # Grid
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            # Padding
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))

        elements.append(table)
        return elements

    def _build_totals(self) -> list:
        """Build totals section."""
        elements = []

        inv = self.invoice
        data = []
        if inv.totals_source == 'LINES':
            # Discount is taken per line before VAT, so it sits above the
            # ex-VAT subtotal.
            if inv.discount > 0:
                data.append(['Total before discount (excl. VAT):', format_zar(inv.subtotal + inv.discount, minus='-')])
                data.append(['Discount:', format_zar(-inv.discount, minus='-')])
            data.append(['Subtotal (excl. VAT):', format_zar(inv.subtotal, minus='-')])
            data.append(['VAT (15%):' if inv.vat_amount else 'VAT:', format_zar(inv.vat_amount, minus='-')])
        else:
            data.append(['Subtotal:', format_zar(inv.subtotal, minus='-')])
            data.append(['VAT (15%):', format_zar(inv.vat_amount, minus='-')])
            if inv.discount > 0:
                data.append(['Discount:', format_zar(-inv.discount, minus='-')])

        total_row = len(data)
        data.append(['TOTAL (incl. VAT):' if inv.vat_amount else 'TOTAL:', format_zar(inv.total_amount, minus='-')])

        has_balance = inv.paid_amount > 0 or inv.credited_amount > 0
        if inv.credited_amount > 0:
            data.append(['Credited:', format_zar(-inv.credited_amount, minus='-')])
        if inv.paid_amount > 0:
            data.append(['Paid:', format_zar(inv.paid_amount, minus='-')])
        if has_balance:
            data.append(['Balance Due:', format_zar(inv.balance, minus='-')])

        bold_rows = [total_row]
        if has_balance:
            bold_rows.append(len(data) - 1)

        style = [
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ]
        for r in bold_rows:
            style += [
                ('FONTNAME', (0, r), (-1, r), 'Helvetica-Bold'),
                ('FONTSIZE', (0, r), (-1, r), 12),
                ('LINEABOVE', (0, r), (-1, r), 1.5, colors.HexColor('#1e3a8a')),
                ('TOPPADDING', (0, r), (-1, r), 8),
            ]

        table = Table(data, colWidths=[140*mm, 35*mm])
        table.setStyle(TableStyle(style))

        elements.append(table)
        return elements

    def _build_footer(self) -> list:
        """Build PDF footer with payment details."""
        elements = []

        # Payment terms
        if self.invoice.notes:
            elements.append(Paragraph("<b>Notes:</b>", self.styles['SectionHeader']))
            elements.append(Paragraph(self.invoice.notes, self.styles['Normal']))
            elements.append(Spacer(1, 5*mm))

        # How to pay — the company's own bank details when it has set them
        # (Settings > Company > Banking details); otherwise the old wording
        # asking the customer to contact the company. Values are escaped:
        # Paragraph parses its text as markup.
        from xml.sax.saxutils import escape as _esc
        from core.services.payment_details import (
            company_bank_details, bank_detail_rows, reference_text,
        )
        company = getattr(self.invoice, 'company', None)
        company_name = company.company_name if company else 'the company'
        bank = company_bank_details(company)
        if bank:
            elements.append(Paragraph("<b>HOW TO PAY:</b>", self.styles['SectionHeader']))
            elements.append(Paragraph(
                "<br/>".join(f"{_esc(label)}: <b>{_esc(value)}</b>"
                             for label, value in bank_detail_rows(bank)),
                self.styles['Normal']
            ))
            elements.append(Spacer(1, 3*mm))
            footer_text = (f"Reference: <b>{_esc(self.invoice.invoice_number or '')}</b> — "
                           f"{_esc(reference_text(bank, self.invoice.invoice_number))} "
                           "Thank you for your business!")
        else:
            elements.append(Paragraph("<b>BANKING DETAILS:</b>", self.styles['SectionHeader']))
            elements.append(Paragraph(
                f"Please contact {_esc(company_name)} for banking details.",
                self.styles['Normal']
            ))
            footer_text = "Please use the invoice number as payment reference. Thank you for your business!"
        elements.append(Spacer(1, 5*mm))

        # Footer text
        elements.append(Paragraph(footer_text, self.styles['Normal']))

        # Platform attribution
        elements.append(Spacer(1, 6*mm))
        elements.append(Paragraph(
            "Powered by TruckWys",
            ParagraphStyle('Attribution', parent=self.styles['Normal'],
                           fontSize=7, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)
        ))

        return elements

    def _save_to_file(self) -> str:
        """
        Save the PDF buffer to a file.

        Returns:
            str: Relative path to the saved file
        """
        # Create directory if it doesn't exist
        invoice_dir = os.path.join(
            settings.MEDIA_ROOT,
            'invoices',
            str(self.invoice.issue_date.year),
            str(self.invoice.issue_date.month).zfill(2)
        )
        os.makedirs(invoice_dir, exist_ok=True)

        # Generate filename
        filename = f"{self.invoice.invoice_number}.pdf"
        filepath = os.path.join(invoice_dir, filename)

        # Write buffer to file
        with open(filepath, 'wb') as f:
            f.write(self.buffer.getvalue())

        # Return relative path
        relative_path = os.path.join(
            'invoices',
            str(self.invoice.issue_date.year),
            str(self.invoice.issue_date.month).zfill(2),
            filename
        )

        return relative_path

    @classmethod
    def generate_pdf(cls, invoice: Invoice) -> str:
        """
        Convenience method to generate and save PDF for an invoice.

        Args:
            invoice: Invoice to generate PDF for

        Returns:
            str: Path to the saved PDF file
        """
        generator = cls(invoice)
        return generator.generate()
