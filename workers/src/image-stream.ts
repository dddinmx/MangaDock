import { HttpError } from "./index";
export function imageMime(bytes: Uint8Array) {
  const ascii = (start: number, end: number) =>
    String.fromCharCode(...bytes.subarray(start, end));
  if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255)
    return "image/jpeg";
  if (ascii(1, 4) === "PNG" && bytes[0] === 137) return "image/png";
  if (["GIF87a", "GIF89a"].includes(ascii(0, 6))) return "image/gif";
  if (ascii(0, 4) === "RIFF" && ascii(8, 12) === "WEBP") return "image/webp";
  if (ascii(4, 8) === "ftyp" && ["avif", "avis"].includes(ascii(8, 12)))
    return "image/avif";
  if (ascii(0, 2) === "BM") return "image/bmp";
  return null;
}
export async function imageResponse(response: Response) {
  if (!response.body) throw new HttpError(502, "图片为空");
  const reader = response.body.getReader(),
    prefix: Uint8Array[] = [];
  let size = 0;
  while (size < 16) {
    const part = await reader.read();
    if (part.done) break;
    prefix.push(part.value);
    size += part.value.byteLength;
  }
  const head = new Uint8Array(size);
  let offset = 0;
  for (const part of prefix) {
    head.set(part, offset);
    offset += part.byteLength;
  }
  const mime = imageMime(head);
  if (!mime) {
    await reader.cancel();
    throw new HttpError(502, "来源图片格式无效");
  }
  let first = true;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (first) {
        first = false;
        controller.enqueue(head);
        return;
      }
      const part = await reader.read();
      if (part.done) controller.close();
      else controller.enqueue(part.value);
    },
    cancel: (reason) => reader.cancel(reason),
  });
  const headers = new Headers(response.headers);
  headers.set("Content-Type", mime);
  return new Response(stream, { status: response.status, headers });
}
