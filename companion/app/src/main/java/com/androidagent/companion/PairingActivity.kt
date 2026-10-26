package com.androidagent.companion

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * The only screen in the app. It shows whether the service is running,
 * hands the owner the pairing token to paste into the agent's .env, and
 * links to the system Accessibility settings, which is the one place the
 * service can be enabled from.
 */
class PairingActivity : AppCompatActivity() {

    private lateinit var status: TextView
    private lateinit var tokenView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        root.addView(heading("Android Agent Companion"))
        root.addView(
            body(
                "This app lets your agent read the screen and tap controls for " +
                    "the workflows you ask it to run. It does nothing on its own " +
                    "and sends nothing off the device.",
            ),
        )

        status = body("")
        root.addView(status)

        root.addView(heading("1. Enable the service"))
        root.addView(
            body("Find \"Android Agent\" under Installed apps or Downloaded apps and turn it on."),
        )
        root.addView(
            Button(this).apply {
                text = "Open Accessibility settings"
                setOnClickListener {
                    startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                }
            },
        )

        root.addView(heading("2. Pair the agent"))
        root.addView(
            body("Add this line to the agent's .env file in Termux, then restart it:"),
        )
        tokenView = body("").apply {
            setTextIsSelectable(true)
            typeface = android.graphics.Typeface.MONOSPACE
        }
        root.addView(tokenView)
        root.addView(
            Button(this).apply {
                text = "Copy to clipboard"
                setOnClickListener { copyToken() }
            },
        )
        root.addView(
            body(
                "The first program that connects with this token is locked in. " +
                    "If you ever reinstall or re-pair the agent, reset the token below.",
            ),
        )
        root.addView(
            Button(this).apply {
                text = "Reset pairing token"
                setOnClickListener {
                    Pairing.reset(this@PairingActivity)
                    refresh()
                    toast("New token generated. Update the agent's .env.")
                }
            },
        )

        setContentView(
            ScrollView(this).apply {
                addView(
                    root,
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                )
            },
        )
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        val connected = AgentAccessibilityService.instance != null
        status.text = if (connected) {
            "Status: running and listening."
        } else {
            "Status: not running. Enable the service in step 1."
        }
        status.gravity = Gravity.START
        tokenView.text = "ANDROID_AGENT_COMPANION_TOKEN=${Pairing.token(this)}"
    }

    private fun copyToken() {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(
            ClipData.newPlainText("companion token", tokenView.text.toString()),
        )
        toast("Copied.")
    }

    private fun toast(message: String) =
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()

    private fun heading(text: String) = TextView(this).apply {
        this.text = text
        textSize = 18f
        setPadding(0, (24 * resources.displayMetrics.density).toInt(), 0, 8)
    }

    private fun body(text: String) = TextView(this).apply {
        this.text = text
        textSize = 15f
        setPadding(0, 4, 0, 8)
    }
}
