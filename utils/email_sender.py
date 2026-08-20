import os
import requests as http_requests

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Campus Clash <noreply@campusclash.in>")


def send_reset_email(to_email: str, reset_url: str) -> bool:
    """Send a password-reset email via Resend HTTP API.

    Returns True on success, False on failure.
    """
    if not RESEND_API_KEY:
        print("[email_sender] RESEND_API_KEY not set — skipping email")
        return False

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    </head>
    <body style="margin:0;padding:0;background:#0a0a1a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
      <div style="max-width:480px;margin:40px auto;background:#12122a;border-radius:16px;border:1px solid #2a2a4a;overflow:hidden;">
        <div style="padding:32px 32px 0;text-align:center;">
          <div style="font-size:24px;font-weight:800;text-transform:uppercase;letter-spacing:0.05em;color:#fff;">
            Campus <span style="color:#a855f7;">Clash</span>
          </div>
          <div style="width:40px;height:2px;background:linear-gradient(90deg,#a855f7,#6366f1);margin:12px auto;border-radius:999px;"></div>
        </div>
        <div style="padding:24px 32px 32px;">
          <h2 style="font-size:20px;font-weight:700;color:#fff;margin:0 0 12px;">Reset Your Password</h2>
          <p style="font-size:14px;color:#a0a0c0;margin:0 0 24px;line-height:1.6;">
            We received a request to reset the password for your Campus Clash account.
            Click the button below to set a new password. This link expires in <strong style="color:#fff;">15 minutes</strong>.
          </p>
          <a href="{reset_url}"
             style="display:block;text-align:center;padding:14px 24px;background:linear-gradient(135deg,#a855f7,#6366f1);color:#fff;font-size:15px;font-weight:700;text-decoration:none;border-radius:10px;text-transform:uppercase;letter-spacing:0.06em;">
            Reset Password
          </a>
          <p style="font-size:13px;color:#6b6b8d;margin:24px 0 0;line-height:1.5;">
            If you didn't request this, you can safely ignore this email. Your password won't change until you click the link above.
          </p>
        </div>
        <div style="padding:16px 32px;border-top:1px solid #2a2a4a;text-align:center;">
          <p style="font-size:11px;color:#4a4a6a;margin:0;">Campus Clash &mdash; Enter The Arena</p>
        </div>
      </div>
    </body>
    </html>
    """

    try:
        resp = http_requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": FROM_EMAIL,
                "to": [to_email],
                "subject": "Reset Your Campus Clash Password",
                "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return True
        print(f"[email_sender] Resend returned {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        print(f"[email_sender] Failed to send email: {e}")
        return False
