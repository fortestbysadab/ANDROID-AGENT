package com.androidagent.companion

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * The whole device-side surface of the companion.
 *
 * It exposes a small, fixed set of operations. There is no "run this
 * arbitrary gesture" entry point, and nothing here decides what to do: the
 * agent on the Termux side drives it one bounded step at a time.
 */
class AgentAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "AgentService"
        private const val MAX_NODES = 400
        private const val MAX_DEPTH = 40
        private const val MAIN_TIMEOUT_MS = 5_000L
        private const val TEXT_CAP = 500

        @Volatile
        var instance: AgentAccessibilityService? = null
            private set
    }

    private var bridge: Bridge? = null
    private val main = Handler(Looper.getMainLooper())
    private val sequence = AtomicInteger(0)

    /** Nodes from the most recent dump, addressable by index for one snapshot. */
    private var snapshotSeq = -1
    private var snapshotNodes: List<AccessibilityNodeInfo> = emptyList()

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        bridge = Bridge(this) { op, args -> onMain { execute(op, args) } }.also { it.start() }
        Log.i(TAG, "connected")
    }

    override fun onUnbind(intent: Intent?): Boolean {
        bridge?.stop()
        bridge = null
        instance = null
        Log.i(TAG, "unbound")
        return super.onUnbind(intent)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Intentionally empty. The companion never reacts to events on its
        // own; it only answers authenticated requests. Events are declared
        // in the config purely because the platform requires a feedback type.
    }

    override fun onInterrupt() {}

    /** Runs the work on the main looper and waits for it, so node access is serialised. */
    private fun onMain(block: () -> JSONObject): JSONObject {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        var result: JSONObject = error("timeout", "device did not respond in time")
        val latch = CountDownLatch(1)
        main.post {
            try {
                result = block()
            } catch (e: Exception) {
                result = error("internal_error", e.message ?: e.javaClass.simpleName)
            } finally {
                latch.countDown()
            }
        }
        if (!latch.await(MAIN_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
            return error("timeout", "device did not respond in time")
        }
        return result
    }

    private fun execute(op: String, args: JSONObject): JSONObject = when (op) {
        "ping" -> ok(JSONObject().put("ready", true))
        "dump" -> dump()
        "click" -> actOnNode(args, AccessibilityNodeInfo.ACTION_CLICK)
        "long_click" -> actOnNode(args, AccessibilityNodeInfo.ACTION_LONG_CLICK)
        "set_text" -> setText(args)
        "scroll" -> scroll(args)
        "global" -> globalAction(args)
        "launch" -> launch(args)
        else -> error("unknown_op", "no such operation: $op")
    }

    // ---- operations -------------------------------------------------

    private fun dump(): JSONObject {
        val root = rootInActiveWindow
            ?: return error("no_window", "no active window to read")
        val nodes = ArrayList<AccessibilityNodeInfo>()
        collect(root, 0, nodes)
        val seq = sequence.incrementAndGet()
        snapshotSeq = seq
        snapshotNodes = nodes

        val elements = JSONArray()
        nodes.forEachIndexed { index, node ->
            val bounds = Rect().also { node.getBoundsInScreen(it) }
            elements.put(
                JSONObject()
                    .put("index", index)
                    .put("text", cap(node.text?.toString()))
                    .put("desc", cap(node.contentDescription?.toString()))
                    .put("id", node.viewIdResourceName ?: "")
                    .put("cls", node.className?.toString() ?: "")
                    .put("pkg", node.packageName?.toString() ?: "")
                    .put("clickable", node.isClickable)
                    .put("editable", node.isEditable)
                    .put("scrollable", node.isScrollable)
                    .put("checked", node.isChecked)
                    .put("enabled", node.isEnabled)
                    .put("focused", node.isFocused)
                    .put(
                        "bounds",
                        JSONArray().put(bounds.left).put(bounds.top)
                            .put(bounds.right).put(bounds.bottom),
                    ),
            )
        }
        return ok(
            JSONObject()
                .put("seq", seq)
                .put("package", root.packageName?.toString() ?: "")
                .put("truncated", nodes.size >= MAX_NODES)
                .put("elements", elements),
        )
    }

    private fun collect(node: AccessibilityNodeInfo?, depth: Int, into: ArrayList<AccessibilityNodeInfo>) {
        if (node == null || depth > MAX_DEPTH || into.size >= MAX_NODES) return
        // Invisible nodes linger in the tree and are a classic source of
        // phantom matches, so they never reach the agent.
        if (!node.isVisibleToUser) return
        into.add(node)
        for (i in 0 until node.childCount) {
            collect(node.getChild(i), depth + 1, into)
        }
    }

    private fun resolve(args: JSONObject): Pair<AccessibilityNodeInfo?, JSONObject?> {
        val seq = args.optInt("seq", -1)
        if (seq != snapshotSeq) {
            return null to error("stale_reference", "the screen was re-read since that element was found")
        }
        val index = args.optInt("index", -1)
        if (index < 0 || index >= snapshotNodes.size) {
            return null to error("stale_reference", "element $index is not in the current screen")
        }
        return snapshotNodes[index] to null
    }

    /** Walks up to the nearest ancestor that actually accepts the action. */
    private fun actionable(node: AccessibilityNodeInfo, action: Int): AccessibilityNodeInfo? {
        var current: AccessibilityNodeInfo? = node
        var hops = 0
        while (current != null && hops < 8) {
            val accepts = when (action) {
                AccessibilityNodeInfo.ACTION_CLICK -> current.isClickable
                AccessibilityNodeInfo.ACTION_LONG_CLICK -> current.isLongClickable
                else -> true
            }
            if (accepts && current.isEnabled) return current
            current = current.parent
            hops++
        }
        return null
    }

    private fun actOnNode(args: JSONObject, action: Int): JSONObject {
        val (node, failure) = resolve(args)
        if (failure != null) return failure
        val target = actionable(node!!, action)
            ?: return error("not_actionable", "that element and its parents cannot be tapped")
        val performed = target.performAction(action)
        return if (performed) ok(JSONObject().put("performed", true))
        else error("action_failed", "the device refused the action")
    }

    private fun setText(args: JSONObject): JSONObject {
        val (node, failure) = resolve(args)
        if (failure != null) return failure
        val value = args.optString("text", "")
        if (value.length > TEXT_CAP) {
            return error("invalid_argument", "text is longer than $TEXT_CAP characters")
        }
        if (!node!!.isEditable) return error("not_actionable", "that element is not a text field")
        val bundle = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, value)
        }
        val performed = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)
        return if (performed) ok(JSONObject().put("performed", true))
        else error("action_failed", "the device refused to set the text")
    }

    private fun scroll(args: JSONObject): JSONObject {
        val forward = args.optString("direction", "forward") != "backward"
        val action = if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
        else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        if (args.has("index")) {
            val (node, failure) = resolve(args)
            if (failure != null) return failure
            return if (node!!.performAction(action)) ok(JSONObject().put("performed", true))
            else error("action_failed", "that element did not scroll")
        }
        val root = rootInActiveWindow ?: return error("no_window", "no active window to scroll")
        val scrollable = findScrollable(root, 0)
            ?: return error("not_actionable", "nothing on this screen scrolls")
        return if (scrollable.performAction(action)) ok(JSONObject().put("performed", true))
        else error("action_failed", "the screen did not scroll, it may be at the end")
    }

    private fun findScrollable(node: AccessibilityNodeInfo?, depth: Int): AccessibilityNodeInfo? {
        if (node == null || depth > MAX_DEPTH) return null
        if (node.isScrollable && node.isVisibleToUser) return node
        for (i in 0 until node.childCount) {
            findScrollable(node.getChild(i), depth + 1)?.let { return it }
        }
        return null
    }

    private fun globalAction(args: JSONObject): JSONObject {
        val action = when (args.optString("action", "")) {
            "back" -> GLOBAL_ACTION_BACK
            "home" -> GLOBAL_ACTION_HOME
            "recents" -> GLOBAL_ACTION_RECENTS
            "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
            else -> return error("invalid_argument", "unsupported global action")
        }
        return if (performGlobalAction(action)) ok(JSONObject().put("performed", true))
        else error("action_failed", "the device refused the action")
    }

    private fun launch(args: JSONObject): JSONObject {
        val packageName = args.optString("package", "")
        if (packageName.isEmpty()) return error("invalid_argument", "missing package")
        val intent = packageManager.getLaunchIntentForPackage(packageName)
            ?: return error("app_not_installed", "$packageName is not installed")
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        startActivity(intent)
        return ok(JSONObject().put("launched", packageName))
    }

    private fun cap(value: String?): String {
        val text = value ?: return ""
        return if (text.length <= TEXT_CAP) text else text.take(TEXT_CAP) + "…"
    }
}
