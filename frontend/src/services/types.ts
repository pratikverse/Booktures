export type BookStatus =
  | "queued"
  | "uploaded"
  | "processing"
  | "analyzed"
  | "generating_images"
  | "completed"
  | "ready"
  | "failed";

export type JobStatus =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "cancelled"
  | "failed";

export type JobType = "book_pipeline" | "image_generation" | string;

export interface Book {
  id: number;
  title: string;
  status: BookStatus;
  progress: number;
  file_path?: string;
}

export interface Page {
  page: number;
  content: string;
  summary: string;
  characters: string;
  scenes: string;
  illustration_url?: string;
  image_prompt?: string;
}

export interface BookContent {
  book_id: number;
  total_pages: number;
  pages: Page[];
}

export interface Job {
  id: number;
  book_id: number;
  bookTitle?: string;
  type: JobType;
  label?: string;
  status: JobStatus;
  progress: number;
  createdAt?: string;
  note?: string;
}

export interface Character {
  id: number;
  book_id: number;
  name: string;
  aliases: string;
  visual_profile: string;
  mention_count: number;
  page_numbers: number[];
}

export interface Settings {
  llmProvider: string;
  llmModel: string;
  groqModel: string;
  geminiModel: string;
  imageProvider: string;
  ollamaUrl: string;
  modelName: string;
  timeout: number;
  imageMode: "quality" | "balanced" | "fast" | "custom";
  imageModel: string;
  imageWidth: number;
  imageHeight: number;
  imageSteps: number;
  imageGuidance: number;
  imageStyle: "normal" | "storybook" | "comic" | "cinematic";
}

export const MODE_PRESETS: Record<
  Exclude<Settings["imageMode"], "custom">,
  Pick<Settings, "imageModel" | "imageWidth" | "imageHeight" | "imageSteps" | "imageGuidance">
> = {
  quality: {
    imageModel: "SG161222/RealVisXL_V4.0",
    imageWidth: 768,
    imageHeight: 768,
    imageSteps: 24,
    imageGuidance: 5.5,
  },
  balanced: {
    imageModel: "segmind/SSD-1B",
    imageWidth: 768,
    imageHeight: 768,
    imageSteps: 12,
    imageGuidance: 8,
  },
  fast: {
    imageModel: "stabilityai/sd-turbo",
    imageWidth: 512,
    imageHeight: 768,
    imageSteps: 4,
    imageGuidance: 1.5,
  },
};
