"""
Shared image upload helper.

Render's free-tier filesystem is ephemeral — anything saved locally under
uploads/ is wiped on every redeploy/restart/spin-down. Cloudinary stores the
file externally and gives back a permanent URL, so uploaded images (payment
screenshots, tournament banners, etc.) survive restarts.

Requires these env vars (see README / .env.example):
    CLOUDINARY_CLOUD_NAME
    CLOUDINARY_API_KEY
    CLOUDINARY_API_SECRET
"""

import os
import cloudinary
import cloudinary.uploader

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)


def is_configured():
    """True once Cloudinary env vars are actually set."""
    return bool(
        os.getenv("CLOUDINARY_CLOUD_NAME")
        and os.getenv("CLOUDINARY_API_KEY")
        and os.getenv("CLOUDINARY_API_SECRET")
    )


def upload_image(file, folder):
    """
    Upload a werkzeug FileStorage object to Cloudinary.
    Returns the permanent secure_url (string) to store in Mongo.
    Always raises RuntimeError (never lets the raw Cloudinary/network
    exception escape), so callers can turn it into a clean error response
    with the real reason instead of a bare 500.
    """
    if not is_configured():
        raise RuntimeError(
            "Cloudinary is not configured — set CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET."
        )

    try:
        result = cloudinary.uploader.upload(file, folder=folder)
    except Exception as e:
        raise RuntimeError(f"Image upload failed: {e}")

    return result["secure_url"]
