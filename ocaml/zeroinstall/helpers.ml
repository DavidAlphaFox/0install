(* Copyright (C) 2013, Thomas Leonard
 * See the README file for details, or visit http://0install.net.
 *)

(** High-level helper functions *)

open General
open Support.Common
module Basedir = Support.Basedir
module R = Requirements
module U = Support.Utils
module Q = Support.Qdom

type select_mode = [
  | `Select_only       (* only download feeds, not archives; display "Select" in GUI *)
  | `Download_only     (* download archives too; refresh if stale feeds; display "Download" in GUI *)
  | `Select_for_run    (* download archives; update stale in background; display "Run" in GUI *)
  | `Select_for_update (* like Download_only, but save changes to apps *)
]

(** Ensure all selections are cached, downloading any that are missing.
    If [distro] is given then distribution packages are also installed, otherwise
    they are ignored. *)
let download_selections fetcher ?distro sels =
  match Lwt_main.run @@ fetcher#download_selections ?distro sels with
  | `success -> ()
  | `aborted_by_user -> raise_safe "Aborted by user"

(** Get some selectsions for these requirements.
    Returns [None] if the user cancels.
    @raise Safe_exception if the solve fails. *)
let solve_and_download_impls (driver:Driver.driver) ?test_callback reqs mode ~refresh ~use_gui =
  let config = driver#config in
  let use_gui =
    match use_gui, config.dry_run with
    | Yes, true -> raise_safe "Can't use GUI with --dry-run"
    | (Maybe|No), true -> No
    | use_gui, false -> use_gui in

  let solve_without_gui () =
    let result = driver#solve_with_downloads reqs ~force:refresh ~update_local:refresh in
    match result with
    | (false, result) -> raise_safe "%s" (Diagnostics.get_failure_reason config result)
    | (true, result) ->
        let sels = result#get_selections in
        let () =
          match mode with
          | `Select_only -> ()
          | `Download_only | `Select_for_run ->
              download_selections driver#fetcher ~distro:driver#distro sels in
        Some sels in

  match Gui.get_selections_gui driver ?test_callback mode reqs ~refresh ~use_gui with
  | `Success sels -> Some sels
  | `Aborted_by_user -> None
  | `Dont_use_GUI -> solve_without_gui ()

(** Convenience wrapper for Fetch.download_and_import_feed that just gives the final result.
 * If the mirror replies first, but the primary succeeds, we return the primary.
 *)
let download_and_import_feed fetcher url =
  let `remote_feed feed_url = url in
  let update = ref None in
  let rec wait_for (result:Fetch.fetch_feed_response Lwt.t) =
    match_lwt result with
    | `update (feed, None) -> `success feed |> Lwt.return
    | `update (feed, Some next) ->
        update := Some feed;
        wait_for next
    | `aborted_by_user -> Lwt.return `aborted_by_user
    | `no_update -> (
        match !update with
        | None -> Lwt.return `no_update
        | Some update -> Lwt.return (`success update)  (* Use the previous partial update *)
    )
    | `problem (msg, None) -> (
        match !update with
        | None -> raise_safe "%s" msg
        | Some update ->
            log_warning "Feed %s: %s" feed_url msg;
            Lwt.return (`success update)  (* Use the previous partial update *)
    )
    | `problem (msg, Some next) ->
        log_warning "Feed '%s': %s" feed_url msg;
        wait_for next in

  wait_for @@ fetcher#download_and_import_feed url
