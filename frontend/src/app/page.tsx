import { redirect } from "next/navigation";

// The app is Claude Hardware only: its front page is a new hardware session, not a general chat.
export default function Home() {
  redirect("/new");
}
