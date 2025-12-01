"""
Email service using Mandrill for sending reports and notifications.
"""
import logging
from typing import Optional
import requests

from webapp.config import MANDRILL_API_KEY, FROM_EMAIL, FROM_NAME, BASE_URL

logger = logging.getLogger(__name__)


class MandrillEmailService:
    """Send emails via Mandrill API."""

    def __init__(self):
        self.api_key = MANDRILL_API_KEY
        self.api_url = "https://mandrillapp.com/api/1.0/messages/send.json"

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None
    ) -> bool:
        """
        Send an email via Mandrill.

        Args:
            to_email: Recipient email address
            subject: Email subject
            html_content: HTML body of the email
            text_content: Plain text fallback (optional)

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.api_key:
            logger.error("MANDRILL_API_KEY not configured")
            return False

        payload = {
            "key": self.api_key,
            "message": {
                "from_email": FROM_EMAIL,
                "from_name": FROM_NAME,
                "to": [{"email": to_email, "type": "to"}],
                "subject": subject,
                "html": html_content,
                "text": text_content or self._strip_html(html_content),
                "track_opens": True,
                "track_clicks": True,
            }
        }

        try:
            response = requests.post(self.api_url, json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()

            if result and result[0].get("status") in ["sent", "queued"]:
                logger.info(f"Email sent successfully to {to_email}")
                return True
            else:
                logger.error(f"Email failed: {result}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False

    def _strip_html(self, html: str) -> str:
        """Simple HTML to text conversion."""
        import re
        text = re.sub(r'<[^>]+>', '', html)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def send_verification_email(self, to_email: str, token: str, region: str, frequency: str = "monthly", taxon_filter: str = None) -> bool:
        """Send email verification link to new subscriber."""
        verify_url = f"{BASE_URL}/verify/{token}"

        # Build scope description
        scope_parts = [f"<strong>{region}</strong>"]
        if taxon_filter:
            scope_parts.append(f"filtered to <strong>{taxon_filter}</strong>")
        scope_description = " ".join(scope_parts)

        subject = f"Confirm your invasive species report subscription for {region}"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #74ac00; color: white; padding: 20px; text-align: center; }}
                .content {{ padding: 20px; background: #f9f9f9; }}
                .button {{
                    display: inline-block;
                    background: #74ac00;
                    color: white;
                    padding: 12px 24px;
                    text-decoration: none;
                    border-radius: 4px;
                    margin: 20px 0;
                }}
                .footer {{ padding: 20px; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>iNaturalist Invasives Monitor</h1>
                </div>
                <div class="content">
                    <h2>Confirm Your Subscription</h2>
                    <p>You've requested to receive {frequency} invasive species reports for {scope_description}.</p>
                    <p>Click the button below to confirm your email address and activate your subscription:</p>
                    <p style="text-align: center;">
                        <a href="{verify_url}" class="button">Confirm Subscription</a>
                    </p>
                    <p>Or copy this link: <a href="{verify_url}">{verify_url}</a></p>
                    <p>This link expires in 48 hours.</p>
                </div>
                <div class="footer">
                    <p>If you didn't request this subscription, you can safely ignore this email.</p>
                    <p>Powered by <a href="https://www.inaturalist.org">iNaturalist</a> data.</p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, html_content)

    def send_report_email(
        self,
        to_email: str,
        region: str,
        report_html: str,
        new_species_count: int,
        total_species: int,
        report_uuid: str,
        unsubscribe_token: str,
        frequency: str = "monthly",
        taxon_filter: str = None
    ) -> bool:
        """Send the invasive species report inline in email."""
        unsubscribe_url = f"{BASE_URL}/unsubscribe/{unsubscribe_token}"
        web_report_url = f"{BASE_URL}/report/{report_uuid}"

        freq_label = frequency.title()  # "Weekly" or "Monthly"

        # Build scope description for header
        scope_description = region
        if taxon_filter:
            scope_description = f"{region} ({taxon_filter})"

        if new_species_count > 0:
            subject = f"Alert: {new_species_count} new species detected in {scope_description}"
        else:
            subject = f"{freq_label} report: No new species in {scope_description}"

        # Build scope line for email body
        scope_line = f"<strong>Region:</strong> {region}"
        if taxon_filter:
            scope_line += f" | <strong>Taxon filter:</strong> {taxon_filter}"

        # Wrap the report HTML with email wrapper
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
                .email-header {{ background: #74ac00; color: white; padding: 15px; text-align: center; }}
                .email-scope {{ background: #f5f5f5; padding: 10px 15px; font-size: 13px; color: #555; }}
                .email-footer {{
                    padding: 15px;
                    font-size: 11px;
                    color: #666;
                    text-align: center;
                    border-top: 1px solid #ddd;
                    margin-top: 20px;
                }}
                .email-footer a {{ color: #74ac00; }}
            </style>
        </head>
        <body>
            <div class="email-header">
                <h2 style="margin: 0;">iNaturalist Invasives Monitor</h2>
                <p style="margin: 5px 0 0 0; opacity: 0.9;">{freq_label} Report</p>
            </div>

            <div class="email-scope">
                {scope_line}
            </div>

            <div class="report-content">
                {report_html}
            </div>

            <div class="email-footer">
                <p>
                    <a href="{web_report_url}">View this report online</a> |
                    <a href="{unsubscribe_url}">Unsubscribe</a>
                </p>
                <p>
                    This report was generated using <a href="https://www.inaturalist.org">iNaturalist</a> data.<br>
                    Total species observed: {total_species} | New to region: {new_species_count}
                </p>
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, html_content)

    def send_subscription_confirmed_email(
        self,
        to_email: str,
        region: str,
        frequency: str,
        manage_token: str,
        taxon_filter: str = None
    ) -> bool:
        """Send confirmation that subscription is now active."""
        manage_url = f"{BASE_URL}/manage/{manage_token}"

        # Build scope description
        scope_description = f"<strong>{region}</strong>"
        if taxon_filter:
            scope_description += f" (filtered to <strong>{taxon_filter}</strong>)"

        subject = f"Subscription confirmed: {region} invasive species reports"

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #74ac00; color: white; padding: 20px; text-align: center; }}
                .content {{ padding: 20px; background: #f9f9f9; }}
                .button {{
                    display: inline-block;
                    background: #74ac00;
                    color: white;
                    padding: 10px 20px;
                    text-decoration: none;
                    border-radius: 4px;
                }}
                .footer {{ padding: 20px; font-size: 12px; color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>Subscription Confirmed!</h1>
                </div>
                <div class="content">
                    <h2>You're all set</h2>
                    <p>Your subscription to invasive species reports for {scope_description} is now active.</p>
                    <p><strong>Report frequency:</strong> {frequency.title()}</p>
                    <p>You'll receive your first report at the beginning of the next {frequency} period.</p>
                    <p style="text-align: center; margin-top: 20px;">
                        <a href="{manage_url}" class="button">Manage Subscription</a>
                    </p>
                </div>
                <div class="footer">
                    <p>Reports are generated using <a href="https://www.inaturalist.org">iNaturalist</a> observation data.</p>
                </div>
            </div>
        </body>
        </html>
        """

        return self.send_email(to_email, subject, html_content)


# Global email service instance
email_service = MandrillEmailService()
