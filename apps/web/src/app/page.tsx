import { redirect } from "next/navigation";

// The guided demo opens on the watchlist as of June 30, 2024 (see STORY in lib/format.ts).
export default function Home() {
  redirect("/watchlist");
}
