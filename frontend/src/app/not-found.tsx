import { redirect } from "next/navigation";

/** Any URL the app doesn't have (an old link, a typo) opens a new session instead of a 404 page. */
export default function NotFound() {
  redirect("/new");
}
