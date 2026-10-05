package com.musicmate.tv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognizerIntent;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.Toast;

import java.util.ArrayList;

/**
 * MusicMate on the television: one full-screen WebView showing the jukebox.
 *
 * The web app already is the TV interface -- spatial navigation, big type,
 * the remote's OK and Back -- so this does only what a browser on a TV gets
 * wrong: no address bar, no cursor mode (a WebView hands the D-pad to the
 * page as arrow keys), the screen kept awake, and a home-row icon.
 *
 * Finding the server: the saved address, else "http://music.localdomain/"
 * (the A record on the UniFi gateway), else the machine's LAN address on port
 * 80, else the old port. When every one fails the remote gets a dialog to type
 * one in.
 */
public class MainActivity extends Activity {
    private static final String[] CANDIDATES = {
        "http://music.localdomain/", "http://music/", "http://192.168.1.50/", "http://192.168.1.50:8730/"
    };
    private static final int REQ_VOICE = 4001;
    private WebView web;
    private SharedPreferences prefs;
    private int attempt = 0;
    private boolean loadedOnce = false;
    /** Bumped on every load we start. A failed load reports twice -- the error,
     *  then onPageFinished for the error page -- and each report used to step
     *  to the next address, so every failure skipped one. Only the first
     *  report for the current load counts. */
    private int loadGen = 0, decidedGen = -1;
    private String current = null;
    private final Handler handler = new Handler(Looper.getMainLooper());

    /**
     * The little the page is allowed to ask of the television. A WebView has no
     * speech recognition of its own -- the Web Speech API is a Chrome feature,
     * not a WebView one -- so spoken search has to come through here.
     *
     * Deliberately two methods and no arguments: the page arrives over plain
     * HTTP on the house network, so treat it as something that could be tampered
     * with and give it nothing worth stealing. Opening the system's "speak now"
     * dialog is all this can do.
     */
    private class Bridge {
        @JavascriptInterface public boolean hasVoice() {
            return new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                    .resolveActivity(getPackageManager()) != null;
        }
        @JavascriptInterface public void startVoice() {
            runOnUiThread(new Runnable() { public void run() { askAloud(); } });
        }
    }

    /** The system's speech dialog; the words come back in onActivityResult. */
    private void askAloud() {
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_PROMPT, "Say a song, album or artist");
        i.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
        try {
            startActivityForResult(i, REQ_VOICE);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "This television has no voice search", Toast.LENGTH_LONG).show();
        }
    }

    @Override protected void onActivityResult(int req, int result, Intent data) {
        super.onActivityResult(req, result, data);
        if (req != REQ_VOICE || result != RESULT_OK || data == null) return;
        ArrayList<String> said = data.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS);
        if (said == null || said.isEmpty()) return;
        String q = said.get(0);
        if (q == null || q.trim().length() == 0) return;
        // through JSON so an apostrophe in "Don't Stop Me Now" cannot break the call
        web.evaluateJavascript(
            "(function(){ if (typeof mmVoiceResult === 'function') mmVoiceResult("
            + org.json.JSONObject.quote(q) + "); })();", null);
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("musicmate", MODE_PRIVATE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0b0a0e"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // the jukebox starts songs itself
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // the same user agent the browser would send, plus a marker the page
        // could use to know it is inside the app
        s.setUserAgentString(s.getUserAgentString() + " MusicMateTV/1.0");
        web.addJavascriptInterface(new Bridge(), "MusicMateTV");
        /* Without a chrome client a WebView swallows confirm() and prompt(): confirm
           answers false, prompt null, and no dialog appears. Clear queue, new and
           renamed playlists, deleting one and Restart server all did nothing in
           the app. The stock client shows Android's own dialogs, which the remote
           can drive and the system keyboard can type into. */
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageFinished(WebView v, String url) {
                // Only the jukebox counts as loaded. A server answer that is not the
                // app (a JSON "Not Found" page, once, 12 Sep 2026) must not end the
                // search for the right address.
                final int g = loadGen;
                v.evaluateJavascript("(function(){ return !!document.getElementById('view'); })();", ok -> {
                    if (g != loadGen) return;                       // a newer load has started
                    if ("true".equals(ok)) { loadedOnce = true; decidedGen = g; }
                    else failed(g);
                });
            }
            @Override public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) failed(loadGen);
            }
            @Override public void onReceivedHttpError(WebView v, WebResourceRequest req, android.webkit.WebResourceResponse resp) {
                if (req.isForMainFrame()) failed(loadGen);
            }
            @SuppressWarnings("deprecation")
            @Override public void onReceivedError(WebView v, int code, String desc, String failingUrl) {
                failed(loadGen);                                    // API 21-22 only: main frame only
            }
        });
        setContentView(web);
        hideSystemUi();
        attempt = 0;
        String saved = prefs.getString("url", null);
        if (saved != null && saved.length() > 0) load(saved); else tryNext();
    }

    private void load(String url) {
        loadGen++;
        current = url;
        handler.removeCallbacksAndMessages(null);
        web.loadUrl(url);
    }

    /** One verdict per load. Before the app has ever loaded, a failure moves on
     *  to the next address. After it has, the server is merely away -- restarted,
     *  or the PC asleep -- and the page it was showing is retried every few
     *  seconds; before, the television sat on Chrome's error page for good. */
    private void failed(int g) {
        if (g != loadGen || decidedGen == g) return;
        decidedGen = g;
        if (!loadedOnce) { tryNext(); return; }
        final String again = current;
        handler.postDelayed(() -> { if (g == loadGen && again != null) load(again); }, 4000);
    }

    /** Walk the candidate addresses; the first page that loads wins. */
    private void tryNext() {
        if (loadedOnce) return;
        if (attempt < CANDIDATES.length) {
            load(CANDIDATES[attempt++]);
            return;
        }
        askForAddress();
    }

    private void askForAddress() {
        final EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        in.setText(prefs.getString("url", CANDIDATES[0]));
        new AlertDialog.Builder(this)
            .setTitle("Where is MusicMate?")
            .setMessage("The server did not answer at any of the usual addresses. Type its address, for example http://music.localdomain/ or http://192.168.1.50/")
            .setView(in)
            .setPositiveButton("Connect", (d, w) -> {
                String u = in.getText().toString().trim();
                if (!u.startsWith("http")) u = "http://" + u;
                if (!u.endsWith("/")) u = u + "/";
                prefs.edit().putString("url", u).apply();
                attempt = CANDIDATES.length; loadedOnce = false;
                load(u);
            })
            .setNegativeButton("Try again", (d, w) -> { attempt = 0; loadedOnce = false; tryNext(); })
            .setCancelable(false)
            .show();
    }

    private void hideSystemUi() {
        web.setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    /** Back on the remote is the page's back, never a way out of the app. A long press
     *  of Back offers the address dialog, so a moved server can be reached again. */
    @Override public boolean onKeyLongPress(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) { askForAddress(); return true; }
        return super.onKeyLongPress(keyCode, event);
    }
    /** The remote's search or mic key, where the television hands it to the app
     *  at all -- many send the microphone straight to the system assistant and
     *  the app never hears it, which is why the search screen also has a mic of
     *  its own. KEYCODE_VOICE_ASSIST is API 23, named by number to keep the
     *  build's minimum where it is. */
    @Override public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) { event.startTracking(); return true; }
        if (keyCode == KeyEvent.KEYCODE_SEARCH || keyCode == 231) { askAloud(); return true; }
        return super.onKeyDown(keyCode, event);
    }
    @Override public boolean onKeyUp(int keyCode, KeyEvent event) {
        /* a long press already opened the address dialog: its release is not a
           Back as well (isCanceled does not cover FLAG_CANCELED_LONG_PRESS) */
        if (keyCode == KeyEvent.KEYCODE_BACK && event.isTracking() && !event.isCanceled()
                && (event.getFlags() & KeyEvent.FLAG_CANCELED_LONG_PRESS) == 0) {
            web.evaluateJavascript("(function(){ if (typeof backAction === 'function') backAction(); else history.back(); })();", null);
            return true;
        }
        return super.onKeyUp(keyCode, event);
    }

    @Override protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (web != null) { web.destroy(); web = null; }
        super.onDestroy();
    }

    @Override protected void onResume() { super.onResume(); web.onResume(); hideSystemUi(); }
    /** Leaving the foreground (Home on the remote) silences playback on this device;
     *  a WebView otherwise keeps playing with nothing on screen. A Sonos room is
     *  left alone -- the page decides. */
    @Override protected void onPause() {
        web.evaluateJavascript("(function(){ if (typeof tvSuspend === 'function') tvSuspend(); })();", null);
        web.onPause();
        super.onPause();
    }
}
