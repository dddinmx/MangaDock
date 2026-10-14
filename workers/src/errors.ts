export class HttpError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export class StorageBusyError extends HttpError {
  constructor(public retryAfter: number) {
    super(503, "文件正在上传，请稍后重试");
  }
}
