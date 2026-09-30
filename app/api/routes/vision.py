"""Vision endpoint (Phase 17): ask a question about an uploaded image.

The image is checked, shrunk and passed to the local vision model. It is never stored.
"""

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from app.vision.provider import MAX_IMAGE_BYTES, VisionError, prepare_image

router = APIRouter(prefix="/vision", tags=["vision"])


class VisionAnswer(BaseModel):
    answer: str
    model: str


@router.post("/describe", response_model=VisionAnswer)
async def describe(
    request: Request,
    image: UploadFile,
    question: str = Form("Describe this image. Read any important text exactly.", max_length=500),
) -> VisionAnswer:
    vision = getattr(request.app.state, "vision", None)
    if vision is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Vision is turned off.")
    data = await image.read(MAX_IMAGE_BYTES + 1)
    try:
        answer = await vision.describe(prepare_image(data), question)
    except VisionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return VisionAnswer(answer=answer, model=vision.model)
