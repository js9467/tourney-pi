(function(){
  function goOffline(){
    window.location.href = '/offline';
  }
  function checkConnection(){
    const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    const onWifi = conn && conn.type ? conn.type === 'wifi' : true;
    if(!navigator.onLine || !onWifi){
      goOffline();
    }
  }
  checkConnection();
  window.addEventListener('offline', goOffline);
  const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  if(conn && conn.addEventListener){
    conn.addEventListener('change', checkConnection);
  }
})();
