import json
import httpx
import os
from typing import Optional, List, Union
from pydantic import BaseModel, Field, ConfigDict
from ..utils import get_headers, handle_api_error
from ..config import settings
from urllib.parse import quote

# LinkedIn retires API versions after ~12 months; a stale value returns
# 426 NONEXISTENT_VERSION. Override via env when the next one is retired.
LINKEDIN_VERSION = os.getenv("LINKEDIN_VERSION", "202607")

# --- Models ---

class MentionItem(BaseModel):
    """A single mention to embed in post text."""
    text: str = Field(..., description="The display text in the post to turn into a mention (e.g., 'Shabad Singh').")
    urn: str = Field(..., description="LinkedIn URN: 'urn:li:person:ID' for people or 'urn:li:organization:ID' for companies.")

class PostParams(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    text: str = Field(..., description="The commentary/text content of the post.")
    visibility: str = Field(default="PUBLIC", description="Post visibility: PUBLIC or CONNECTIONS.")
    mentions: Optional[List[MentionItem]] = Field(default=None, description="List of mentions to embed in the post text.")

class ImagePostParams(BaseModel):
    text: str = Field(..., description="The text content.")
    image_source: str = Field(..., description="Local file path or public URL of the image.")
    visibility: str = Field(default="PUBLIC")
    alt_text: Optional[str] = Field(default=None, description="Alt text for the image (accessibility + SEO).")
    mentions: Optional[List[MentionItem]] = Field(default=None, description="List of mentions to embed in the post text.")

class DocumentPostParams(BaseModel):
    text: str = Field(..., description="The commentary/text content of the post.")
    file_source: str = Field(..., description="Local file path or public URL of the PDF. Must be under 10 MB; 4:5 portrait pages read best in-feed.")
    title: str = Field(..., description="Title shown above the carousel in the feed (e.g. 'LEINOS Hard Oil 240 — 8 pages').")
    visibility: str = Field(default="PUBLIC", description="Post visibility: PUBLIC or CONNECTIONS.")
    mentions: Optional[List[MentionItem]] = Field(default=None, description="List of mentions to embed in the post text.")

class CommentParams(BaseModel):
    object_urn: str = Field(..., description="The URN of the post/share to comment on (e.g., urn:li:share:123, urn:li:activity:123)")
    text: str = Field(..., description="The text content of the comment.")

class ReactionParams(BaseModel):
    object_urn: str = Field(..., description="The URN of the content to react to (e.g., urn:li:activity:123, urn:li:share:123)")
    reaction_type: str = Field(default="LIKE", description="Reaction type: LIKE, PRAISE, APPRECIATION, EMPATHY, INTEREST, ENTERTAINMENT")

class UpdatePostParams(BaseModel):
    post_urn: str = Field(..., description="The URN of the post to update.")
    text: str = Field(..., description="The new text content.")
    visibility: str = Field(default="PUBLIC", description="Visibility for the new post: PUBLIC or CONNECTIONS.")

# --- Helper: Mentions ---

def build_mention_commentary(text: str, mentions: Optional[List[MentionItem]]) -> str:
    """Transform text by replacing mention display text with Posts API inline annotation syntax.

    For Posts API, mentions use: @[DisplayName](urn:li:type:id)
    Replacements are done from end to start to preserve positions.
    IMPORTANT: Only works at post CREATION time, not with PARTIAL_UPDATE.
    """
    if not mentions:
        return text

    positioned = []
    for mention in mentions:
        start = text.find(mention.text)
        if start != -1:
            positioned.append((start, mention))

    positioned.sort(key=lambda x: x[0], reverse=True)

    for start, mention in positioned:
        end = start + len(mention.text)
        annotation = f"@[{mention.text}]({mention.urn})"
        text = text[:start] + annotation + text[end:]

    return text

# --- Helper: REST API headers ---

def _rest_headers(headers: dict) -> dict:
    """Add Linkedin-Version header for /rest/ endpoints."""
    return {**headers, "Linkedin-Version": LINKEDIN_VERSION}

def _upload_headers(headers: dict) -> dict:
    """Headers for PUTting binary media to an upload URL.

    Content-Type is mandatory. Without it the client library's default
    (application/x-www-form-urlencoded) is sent and LinkedIn answers 400 with an
    HTML error page instead of JSON, which makes the cause very hard to spot.
    """
    return {
        "Authorization": headers["Authorization"],
        "Content-Type": "application/octet-stream",
    }

# --- Helper: Author URN ---

async def get_author_urn(client: httpx.AsyncClient, headers: dict) -> str:
    """Resolve the authenticated member's person URN.

    Uses /v2/me, NOT /v2/userinfo: userinfo is an OpenID Connect endpoint and
    needs the 'openid' scope. With r_basicprofile it returns 401
    INVALID_ACCESS_TOKEN, which looks like a bad token but is a wrong endpoint.

    Note the member id is per-application — the same person has a different id
    under a different client_id, so it must be fetched, never hardcoded.
    """
    resp = await client.get(f"{settings.api_base}/me", headers=headers)
    resp.raise_for_status()
    person_id = resp.json().get("id")
    if not person_id:
        raise ValueError("Could not resolve person id from /v2/me")
    return f"urn:li:person:{person_id}"

# --- Helper: Image Upload (Images API) ---

async def upload_image(client: httpx.AsyncClient, headers: dict, person_urn: str, image_source: str) -> str:
    """Upload image via LinkedIn Images API.

    1. POST /rest/images?action=initializeUpload → get uploadUrl + image URN
    2. PUT binary to uploadUrl
    3. Return image URN (urn:li:image:XXX)
    """
    rest_base = settings.api_base.replace("/v2", "/rest")
    rest_h = _rest_headers(headers)

    # Step 1: Initialize Upload
    init_resp = await client.post(
        f"{rest_base}/images?action=initializeUpload",
        headers=rest_h,
        json={"initializeUploadRequest": {"owner": person_urn}},
    )
    init_resp.raise_for_status()
    init_data = init_resp.json()

    upload_url = init_data["value"]["uploadUrl"]
    image_urn = init_data["value"]["image"]

    # Step 2: Read image data
    if image_source.startswith("http"):
        img_resp = await client.get(image_source)
        img_resp.raise_for_status()
        image_data = img_resp.content
    else:
        if not os.path.exists(image_source):
            raise FileNotFoundError(f"Image file not found: {image_source}")
        with open(image_source, "rb") as f:
            image_data = f.read()

    # Step 3: Upload binary
    upload_resp = await client.put(upload_url, headers=_upload_headers(headers), content=image_data)
    upload_resp.raise_for_status()

    return image_urn

# --- Helper: Document Upload (Documents API) ---

async def upload_document(client: httpx.AsyncClient, headers: dict, person_urn: str, file_source: str) -> str:
    """Upload a PDF via LinkedIn Documents API and return its urn:li:document:XXX.

    Same three-step shape as images, against /rest/documents.
    """
    rest_base = settings.api_base.replace("/v2", "/rest")

    init_resp = await client.post(
        f"{rest_base}/documents?action=initializeUpload",
        headers=_rest_headers(headers),
        json={"initializeUploadRequest": {"owner": person_urn}},
    )
    init_resp.raise_for_status()
    value = init_resp.json()["value"]
    upload_url = value["uploadUrl"]
    document_urn = value["document"]

    if file_source.startswith("http"):
        doc_resp = await client.get(file_source, timeout=120.0)
        doc_resp.raise_for_status()
        document_data = doc_resp.content
    else:
        if not os.path.exists(file_source):
            raise FileNotFoundError(f"Document file not found: {file_source}")
        with open(file_source, "rb") as f:
            document_data = f.read()

    upload_resp = await client.put(
        upload_url, headers=_upload_headers(headers), content=document_data, timeout=180.0
    )
    upload_resp.raise_for_status()

    return document_urn

# --- Post Implementation (Posts API) ---

async def create_image_post(params: ImagePostParams) -> str:
    """Create a post with an image via Posts API (/rest/posts)."""
    try:
        headers = await get_headers()
        rest_base = settings.api_base.replace("/v2", "/rest")
        rest_h = _rest_headers(headers)

        async with httpx.AsyncClient() as client:
            # 1. Get User ID
            author_urn = await get_author_urn(client, headers)

            # 2. Upload Image via Images API
            image_urn = await upload_image(client, headers, author_urn, params.image_source)

            # 3. Build commentary with inline mentions
            commentary = build_mention_commentary(params.text, params.mentions)

            # 4. Build media content
            media_content = {"id": image_urn}
            if params.alt_text:
                media_content["altText"] = params.alt_text

            # 5. Create Post via Posts API
            payload = {
                "author": author_urn,
                "commentary": commentary,
                "visibility": params.visibility,
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "content": {"media": media_content},
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            }

            resp = await client.post(f"{rest_base}/posts", headers=rest_h, json=payload)
            resp.raise_for_status()

            post_id = resp.headers.get("x-restli-id", "unknown")
            return f"✅ Image Post created successfully.\nID: {post_id}"

    except Exception as e:
        return handle_api_error(e)

async def create_document_post(params: DocumentPostParams) -> str:
    """Create a document (PDF carousel) post via Posts API (/rest/posts).

    LinkedIn renders each PDF page as a swipeable card, keeping the reader
    in-feed. Document posts are ranked largely on dwell time, so a multi-page
    PDF typically out-reaches the same content as plain text.
    """
    try:
        headers = await get_headers()
        rest_base = settings.api_base.replace("/v2", "/rest")

        async with httpx.AsyncClient(timeout=180.0) as client:
            author_urn = await get_author_urn(client, headers)
            document_urn = await upload_document(client, headers, author_urn, params.file_source)
            commentary = build_mention_commentary(params.text, params.mentions)

            payload = {
                "author": author_urn,
                "commentary": commentary,
                "visibility": params.visibility,
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                # 'title' is the label shown above the carousel in the feed.
                "content": {"media": {"id": document_urn, "title": params.title}},
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            }

            resp = await client.post(f"{rest_base}/posts", headers=_rest_headers(headers), json=payload)
            resp.raise_for_status()

            post_id = resp.headers.get("x-restli-id", "unknown")
            return (
                f"✅ Document post created successfully.\n"
                f"ID: {post_id}\n"
                f"URL: https://www.linkedin.com/feed/update/{post_id}/"
            )

    except Exception as e:
        return handle_api_error(e)

async def create_post(params: PostParams) -> str:
    """Create a new text-based update on the user LinkedIn feed via Posts API (/rest/posts)."""
    try:
        headers = await get_headers()
        rest_base = settings.api_base.replace("/v2", "/rest")
        rest_h = _rest_headers(headers)

        async with httpx.AsyncClient() as client:
            # 1. Get User ID
            author = await get_author_urn(client, headers)

            # 2. Build commentary with inline mentions
            commentary = build_mention_commentary(params.text, params.mentions)

            # 3. Construct Payload
            payload = {
                "author": author,
                "commentary": commentary,
                "visibility": params.visibility,
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            }

            # 4. Send Request
            resp = await client.post(f"{rest_base}/posts", headers=rest_h, json=payload)
            resp.raise_for_status()

            post_id = resp.headers.get("x-restli-id", "unknown")
            return f"✅ Post created successfully.\nID: {post_id}"

    except Exception as e:
        return handle_api_error(e)

async def update_post(params: UpdatePostParams) -> str:
    """
    Update a post by deleting the old one and creating a new one.
    (LinkedIn API does not support adding mentions via PARTIAL_UPDATE).
    """
    try:
        delete_result = await delete_post(params.post_urn)

        if "Error" in delete_result:
            return f"Update Failed during deletion step: {delete_result}"

        create_params = PostParams(text=params.text, visibility=params.visibility)
        create_result = await create_post(create_params)

        if "Error" in create_result:
            return f"⚠️ Old post deleted, but creation failed: {create_result}"

        return f"✅ Post updated (Re-created).\nOld ID: {params.post_urn}\n{create_result}"

    except Exception as e:
        return handle_api_error(e)

async def delete_post(post_urn: str) -> str:
    """Delete a LinkedIn post by its URN via Posts API."""
    try:
        headers = await get_headers()
        rest_base = settings.api_base.replace("/v2", "/rest")
        rest_h = {**_rest_headers(headers), "X-RestLi-Method": "DELETE"}
        encoded_urn = quote(post_urn)

        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{rest_base}/posts/{encoded_urn}", headers=rest_h)

            if resp.status_code == 404:
                return f"Error: Post {post_urn} not found."

            resp.raise_for_status()
            return f"✅ Post {post_urn} deleted successfully."

    except Exception as e:
        return handle_api_error(e)

async def get_recent_posts() -> str:
    """List the user's recent posts via Posts API.

    NOTE: the author finder on /rest/posts is gated behind LinkedIn partner
    access — a standard app gets 403 partnerApiPostsExternal.FINDER-author.
    Creating posts works fine; only listing them back is restricted.
    """
    try:
        headers = await get_headers()
        rest_base = settings.api_base.replace("/v2", "/rest")
        rest_h = {**_rest_headers(headers), "X-RestLi-Method": "FINDER"}

        async with httpx.AsyncClient() as client:
            # 1. Get Author URN
            author_urn = await get_author_urn(client, headers)

            # 2. Fetch posts
            encoded_author = quote(author_urn)
            url = f"{rest_base}/posts?author={encoded_author}&q=author&count=10&sortBy=LAST_MODIFIED"

            resp = await client.get(url, headers=rest_h)
            resp.raise_for_status()

            data = resp.json()
            posts = []
            for item in data.get("elements", []):
                posts.append({
                    "id": item.get("id"),
                    "text": item.get("commentary", ""),
                    "created": item.get("createdAt"),
                    "visibility": item.get("visibility"),
                })

            return json.dumps(posts, indent=2)

    except Exception as e:
        return handle_api_error(e)

# --- Comment Implementation (v2 Social Actions — still working) ---

async def create_comment(params: CommentParams) -> str:
    """Create a comment on a share, UGC post, or article."""
    try:
        headers = await get_headers()
        async with httpx.AsyncClient() as client:
            actor_urn = await get_author_urn(client, headers)

            encoded_object = quote(params.object_urn)
            url = f"{settings.api_base}/socialActions/{encoded_object}/comments"

            payload = {
                "actor": actor_urn,
                "message": {"text": params.text},
            }

            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()

            comment_id = resp.json().get("id")
            return f"✅ Comment created successfully.\nID: {comment_id}"

    except Exception as e:
        return handle_api_error(e)

async def create_reaction(params: ReactionParams) -> str:
    """Create a reaction (like) on a LinkedIn post."""
    try:
        headers = await get_headers()
        async with httpx.AsyncClient() as client:
            actor_urn = await get_author_urn(client, headers)

            encoded_object = quote(params.object_urn)
            url = f"{settings.api_base}/socialActions/{encoded_object}/likes"

            payload = {
                "actor": actor_urn,
                "object": params.object_urn,
                "reactionType": params.reaction_type,
            }

            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return f"✅ Reaction ({params.reaction_type}) added successfully to {params.object_urn}"

    except Exception as e:
        return handle_api_error(e)

async def get_post_comments(object_urn: str) -> str:
    """Get comments for a specific post/share."""
    try:
        headers = await get_headers()
        encoded_object = quote(object_urn)
        url = f"{settings.api_base}/socialActions/{encoded_object}/comments"

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

            data = resp.json()
            comments = []
            for item in data.get("elements", []):
                comments.append({
                    "id": item.get("id"),
                    "actor": item.get("actor"),
                    "text": item.get("message", {}).get("text"),
                    "created": item.get("created", {}).get("time"),
                })
            return json.dumps(comments, indent=2)

    except Exception as e:
        return handle_api_error(e)

async def delete_comment(comment_urn: str, object_urn: str) -> str:
    """Delete a comment."""
    try:
        headers = await get_headers()

        encoded_object = quote(object_urn)
        comment_id = comment_urn.split(",")[-1].replace(")", "") if "(" in comment_urn else comment_urn
        encoded_comment_id = quote(comment_id)

        url = f"{settings.api_base}/socialActions/{encoded_object}/comments/{encoded_comment_id}"

        async with httpx.AsyncClient() as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code == 404:
                return "Error: Comment or Object not found."
            resp.raise_for_status()
            return "✅ Comment deleted successfully."

    except Exception as e:
        return handle_api_error(e)
