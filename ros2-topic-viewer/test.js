/*

Small script to test rclnodejs installation. In the future I'd like to remove CLI cmds and use native JS to run ROS2 commands.
 */

const rclnodejs = require('rclnodejs');

rclnodejs.init().then(() => {
    const node = new rclnodejs.Node('test_node');
    console.log('Node initialized successfully.');
}).catch((error) => {
    console.error('Error initializing rclnodejs:', error);
});