import { useState } from "react";
import type { Route } from "./+types/search";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "Natural Language Search - CV Filter" },
    { name: "description", content: "Search candidates using natural language queries" },
  ];
}

type SearchResult = {
  id: string;
  first_name: string;
  last_name: string;
  email: string;
  score: number;
  headline?: string;
  top_skills?: string;  // Changed from string[] to string (comma-separated)
  experience_years?: number;
  primary_location?: string;
  education_count?: number;
  language_count?: number;
  skill_count?: number;
};

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState<"hu" | "en">("hu");
  const [sort, setSort] = useState("score_desc");
  const [isLoading, setIsLoading] = useState(false);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = async () => {
    if (!query.trim()) {
      setError("Please enter a search query");
      return;
    }

    setIsLoading(true);
    setError(null);
    setResults([]);

    const token = localStorage.getItem("access_token");
    if (!token) {
      setError("You must be logged in to search");
      setIsLoading(false);
      return;
    }

    try {
      // Step 1: Parse natural language query
      const nlqResponse = await fetch("/api/nlq/parse/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ query, language }),
      });

      if (!nlqResponse.ok) {
        throw new Error("Failed to parse natural language query");
      }

      const nlqData = await nlqResponse.json();

      // Step 2: Search candidates with parsed filters
      const searchResponse = await fetch("/api/candidates/search/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ ...nlqData.filters, sort }),
      });

      if (!searchResponse.ok) {
        throw new Error("Failed to search candidates");
      }

      const searchData = await searchResponse.json();
      setResults(searchData.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsLoading(false);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      void handleSearch();
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 p-6">
      <div className="mx-auto max-w-6xl">
        <div className="mb-8">
          <h1 className="mb-2 text-3xl font-bold text-slate-100">Natural Language Search</h1>
          <p className="text-slate-400">
            Search candidates using natural language (e.g., "3 years Java experience + English B2")
          </p>
        </div>

      {/* Search Input */}
      <div className="mb-6 rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
        <div className="mb-4 flex gap-4">
          <div className="flex-1">
            <label className="mb-2 block text-sm font-semibold text-slate-300">
              Search Query
            </label>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyPress={handleKeyPress}
              placeholder="e.g., 3 years Java experience + English B2"
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-slate-100 placeholder-slate-500 focus:border-blue-500 focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-300">
              Language
            </label>
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value as "hu" | "en")}
              className="rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-slate-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="hu">Hungarian</option>
              <option value="en">English</option>
            </select>
          </div>
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-300">
              Sort by
            </label>
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value)}
              className="rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-slate-100 focus:border-blue-500 focus:outline-none"
            >
              <option value="score_desc">Score</option>
              <option value="experience_desc">Experience</option>
              <option value="education_desc">Education</option>
              <option value="language_desc">Language</option>
              <option value="skills_desc">Skills</option>
              <option value="name_asc">Name (A-Z)</option>
            </select>
          </div>
        </div>

        <button
          onClick={handleSearch}
          disabled={isLoading}
          className="rounded-lg bg-emerald-600 px-6 py-2 font-semibold text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-700"
        >
          {isLoading ? "Searching..." : "Search"}
        </button>
      </div>

      {/* Error Message */}
      {error && (
        <div className="mb-6 rounded-lg border border-red-800 bg-red-900/20 p-4 text-red-400">
          {error}
        </div>
      )}

      {/* Search Results */}
      {results.length > 0 && (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
          <h2 className="mb-4 text-xl font-semibold">
            Search Results ({results.length})
          </h2>
          <div className="space-y-4">
            {results.map((candidate) => (
              <div
                key={candidate.id}
                className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 transition hover:border-slate-600"
              >
                <div className="mb-2 flex items-start justify-between">
                  <div>
                    <h3 className="text-lg font-semibold text-slate-100">
                      {candidate.first_name} {candidate.last_name}
                    </h3>
                    <p className="text-sm text-slate-400">{candidate.email}</p>
                  </div>
                  <div className="rounded-lg bg-emerald-600/20 px-3 py-1 text-sm font-semibold text-emerald-400 border border-emerald-500/30">
                    Score: {(candidate.score * 100).toFixed(0)}%
                  </div>
                </div>

                {candidate.headline && (
                  <p className="mb-2 text-sm text-slate-300">{candidate.headline}</p>
                )}

                <div className="flex flex-wrap gap-4 text-sm text-slate-400">
                  {candidate.experience_years !== undefined && (
                    <span>📅 {candidate.experience_years} years exp.</span>
                  )}
                  {candidate.primary_location && (
                    <span>📍 {candidate.primary_location}</span>
                  )}
                </div>

                <div className="mt-2 flex flex-wrap gap-4 text-xs text-slate-500">
                  {typeof candidate.education_count === "number" && (
                    <span>Education: {candidate.education_count}</span>
                  )}
                  {typeof candidate.language_count === "number" && (
                    <span>Languages: {candidate.language_count}</span>
                  )}
                  {typeof candidate.skill_count === "number" && (
                    <span>Skills: {candidate.skill_count}</span>
                  )}
                </div>

                {candidate.top_skills && candidate.top_skills.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {candidate.top_skills.split(',').map((skill, idx) => (
                      <span
                        key={idx}
                        className="rounded-md bg-slate-700 px-2 py-1 text-xs font-medium text-slate-300"
                      >
                        {skill.trim()}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* No Results */}
      {!isLoading && results.length === 0 && query && !error && (
        <div className="rounded-xl border border-dashed border-slate-700 p-8 text-center text-slate-500">
          No candidates found matching your query.
        </div>
      )}

      {/* Examples */}
      <div className="mt-8 rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
        <h3 className="mb-3 text-lg font-semibold text-slate-100">Example Queries</h3>
        <div className="space-y-2 text-sm text-slate-400">
          <div
            onClick={() => {
              setQuery("Machine learning engineer with Python TensorFlow PyTorch");
              setLanguage("en");
            }}
            className="cursor-pointer rounded-lg border border-slate-700 p-3 hover:border-slate-600 hover:bg-slate-800/50"
          >
            "Machine learning engineer with Python TensorFlow PyTorch"
          </div>
          <div
            onClick={() => {
              setQuery("Data scientist with NLP and computer vision expertise");
              setLanguage("en");
            }}
            className="cursor-pointer rounded-lg border border-slate-700 p-3 hover:border-slate-600 hover:bg-slate-800/50"
          >
            "Data scientist with NLP and computer vision expertise"
          </div>
          <div
            onClick={() => {
              setQuery("Frontend developer with React and TypeScript");
              setLanguage("en");
            }}
            className="cursor-pointer rounded-lg border border-slate-700 p-3 hover:border-slate-600 hover:bg-slate-800/50"
          >
            "Frontend developer with React and TypeScript"
          </div>
          <div
            onClick={() => {
              setQuery("React developer with Next.js and Redux experience");
              setLanguage("en");
            }}
            className="cursor-pointer rounded-lg border border-slate-700 p-3 hover:border-slate-600 hover:bg-slate-800/50"
          >
            "React developer with Next.js and Redux experience"
          </div>
        </div>
      </div>
      </div>
    </div>
  );
}
