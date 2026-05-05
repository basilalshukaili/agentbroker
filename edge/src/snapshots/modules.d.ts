// Wrangler [[rules]] type="Text" turns these imports into raw string content.
declare module "*.txt" {
  const content: string;
  export default content;
}

declare module "*.yaml" {
  const content: string;
  export default content;
}
