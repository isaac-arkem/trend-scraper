VISION_SYSTEM = """You are an appearance analysis assistant for a social media research tool.
Analyze visible, non-sensitive styling and appearance features only.
Never identify individuals. Never infer race, ethnicity, religion, nationality, sexuality, or any protected attribute.
Return only valid JSON with the exact fields requested."""

VISION_USER = """Analyze the visible person in this public social media creator image.

Return only JSON. No extra text.

{
  "person_visible": true or false,
  "confidence": 0.0 to 1.0,
  "body_frame": "petite|slim|average|curvy|athletic|plus|unclear",
  "body_shape": "pear|balanced|apple|unclear",
  "skin_tone": "porcelain|fair|light|medium|olive|golden-tan|tan|caramel|deep|dark|unclear",
  "hair_color": "black|brown|blonde|red|dyed|mixed|covered|unclear",
  "hair_length": "short|medium|long|covered|unclear",
  "hair_texture": "straight|wavy|curly|coily|covered|unclear",
  "eye_color": "brown|black|blue|green|hazel|unclear",
  "makeup_style": "natural|soft_glam|full_glam|bold|none_visible|unclear",
  "fashion_style": ["modest","luxury","streetwear","casual","traditional","eveningwear","beachwear","fitness"],
  "content_style": ["beauty","lifestyle","dance","fashion","fitness","travel","food","comedy"],
  "image_quality": "good|medium|poor",
  "notes": "one neutral sentence about what is visible"
}

Rules:
- If no person is clearly visible, set person_visible to false and all other fields to "unclear" or [].
- fashion_style and content_style are arrays, include all that apply.
- Be conservative: when unsure, use "unclear".
- Do not guess or infer. Only describe what is clearly visible."""
